from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from memos.graph_dbs.hugegraph import HugeGraphGraphDB
from memos.vec_dbs.index import BaseVectorIndex


MEMORY_ID = "11111111-1111-4111-8111-111111111111"


def _backend_with_vector_index() -> HugeGraphGraphDB:
    backend = HugeGraphGraphDB.__new__(HugeGraphGraphDB)
    backend.user_name = "tenant-1"
    backend._g = MagicMock()
    backend._traverser = MagicMock()
    backend._vector_index = MagicMock(spec=BaseVectorIndex)
    backend._gremlin = MagicMock()
    backend._gremlin.exec.return_value = []
    backend.config = SimpleNamespace(graph="hugegraph")
    return backend


def test_gremlin_uses_public_pyhugegraph_manager():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [[3], [4]]

    assert backend._gremlin_exec("g.V().count()") == [3]
    assert backend._gremlin_exec("g.E().count()") == [4]

    assert backend._gremlin.exec.call_args_list[0].args == ("g.V().count()",)
    assert backend._gremlin.exec.call_args_list[1].args == ("g.E().count()",)


def test_health_checks_graph_and_vector_collection():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[0])

    backend.check_health()

    backend._gremlin_exec.assert_called_once_with("g.V().limit(1).count()")
    backend._vector_index.check_health.assert_called_once_with()


def test_health_rejects_missing_vector_collection():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[0])
    backend._vector_index.check_health.side_effect = ConnectionError(
        "Vector collection unavailable"
    )

    with pytest.raises(ConnectionError, match="Vector collection"):
        backend.check_health()


def test_fulltext_search_uses_hugegraph_text_predicate():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[])

    backend.search_by_fulltext(["needle"], user_name="tenant-1")

    query = backend._gremlin_exec.call_args.args[0]
    assert ".has('memory', Text.contains(\"needle\"))" in query
    assert "textContains" not in query


# ---------------------------------------------------------------------------
# search_by_fulltext full semantics (G5). The Neo4j community baseline returns
# [] here (its own TODO), so this backend intentionally does MORE; every caller
# parameter is honoured or rejected instead of being swallowed.
# ---------------------------------------------------------------------------


def _fulltext_row(node_id: str, **props) -> dict:
    row = {"id": [node_id], "memory": [props.pop("memory", "body")]}
    for key, value in props.items():
        row[key] = [value]
    return row


def test_fulltext_search_or_combines_every_query_word():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[])

    backend.search_by_fulltext(["alpha", "beta"], user_name="tenant-1")

    query = backend._gremlin_exec.call_args.args[0]
    assert ".or(" in query
    assert 'Text.contains("alpha")' in query
    assert 'Text.contains("beta")' in query


def test_fulltext_search_dedups_vertices_the_or_emits_once_per_branch():
    """Live 1.8.0 HStore emits a vertex once per matching or() branch; the
    query dedups server-side and the result loop keeps the first occurrence."""
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            _fulltext_row(MEMORY_ID, user_name="tenant-1"),
            _fulltext_row(MEMORY_ID, user_name="tenant-1"),
        ]
    )

    results = backend.search_by_fulltext(["alpha", "beta"], user_name="tenant-1")

    assert [r["id"] for r in results] == [MEMORY_ID]
    assert ".dedup()" in backend._gremlin_exec.call_args.args[0]


def test_fulltext_search_defaults_status_to_activated_and_honours_scope():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[])

    backend.search_by_fulltext(["needle"], user_name="tenant-1", scope="LongTermMemory")

    query = backend._gremlin_exec.call_args.args[0]
    assert ".has('status', \"activated\")" in query
    assert ".has('memory_type', \"LongTermMemory\")" in query

    backend.search_by_fulltext(["needle"], user_name="tenant-1", status="archived")
    assert ".has('status', \"archived\")" in backend._gremlin_exec.call_args.args[0]


def test_fulltext_search_returns_empty_without_calling_gremlin_for_no_words():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[])

    assert backend.search_by_fulltext([], user_name="tenant-1") == []
    assert backend.search_by_fulltext(["", "  "], user_name="tenant-1") == []
    backend._gremlin_exec.assert_not_called()


def test_fulltext_search_post_filters_candidates_by_graph_filter():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            _fulltext_row(
                MEMORY_ID,
                memory="important",
                user_name="tenant-1",
                memory_type="LongTermMemory",
                metadata_json='{"importance": 5}',
            ),
            _fulltext_row(
                "22222222-2222-4222-8222-222222222222",
                memory="trivial",
                user_name="tenant-1",
                memory_type="WorkingMemory",
                metadata_json='{"importance": 1}',
            ),
        ]
    )

    results = backend.search_by_fulltext(
        ["needle"],
        user_name="tenant-1",
        filter={"and": [{"memory_type": "LongTermMemory"}, {"importance": {"gte": 3}}]},
    )
    assert [r["id"] for r in results] == [MEMORY_ID]

    results = backend.search_by_fulltext(
        ["needle"],
        user_name="tenant-1",
        filter={"or": [{"memory_type": "WorkingMemory"}, {"importance": {"gte": 3}}]},
    )
    assert {r["id"] for r in results} == {MEMORY_ID, "22222222-2222-4222-8222-222222222222"}


def test_fulltext_search_post_filters_candidates_by_knowledgebase_ids():
    """kb ids act as additional user names, ORed into the tenant clause."""
    other_id = "22222222-2222-4222-8222-222222222222"
    stranger_id = "33333333-3333-4333-8333-333333333333"
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            _fulltext_row(MEMORY_ID, user_name="tenant-1"),
            _fulltext_row(other_id, user_name="kb-7"),
            _fulltext_row(stranger_id, user_name="tenant-9"),
        ]
    )

    results = backend.search_by_fulltext(
        ["needle"], user_name="tenant-1", knowledgebase_ids=["kb-7"]
    )

    assert {r["id"] for r in results} == {MEMORY_ID, other_id}
    query = backend._gremlin_exec.call_args.args[0]
    assert ".or(" in query
    assert ".has('user_name', \"kb-7\")" in query


def test_fulltext_search_applies_search_filter_as_equality():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            _fulltext_row(MEMORY_ID, user_name="tenant-1", mem_cube_id="kb-1"),
            _fulltext_row(
                "22222222-2222-4222-8222-222222222222", user_name="tenant-1", mem_cube_id="kb-2"
            ),
        ]
    )

    results = backend.search_by_fulltext(
        ["needle"], user_name="tenant-1", search_filter={"mem_cube_id": "kb-1"}
    )

    assert [r["id"] for r in results] == [MEMORY_ID]


def test_fulltext_search_threshold_trims_the_constant_match_score():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[_fulltext_row(MEMORY_ID, user_name="tenant-1")])

    results = backend.search_by_fulltext(["needle"], user_name="tenant-1", threshold=1.0)
    assert [r["id"] for r in results] == [MEMORY_ID]

    assert backend.search_by_fulltext(["needle"], user_name="tenant-1", threshold=1.1) == []


def test_fulltext_search_honours_top_k_after_the_post_filters():
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(11, 16)]
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[_fulltext_row(i, user_name="t") for i in ids])

    results = backend.search_by_fulltext(["needle"], user_name="t", top_k=3)

    assert [r["id"] for r in results] == ids[:3]
    assert ".limit(3)" in backend._gremlin_exec.call_args.args[0]


def test_fulltext_search_rejects_tsquery_config_instead_of_swallowing_it():
    backend = _backend_with_vector_index()

    with pytest.raises(ValueError, match="tsquery_config"):
        backend.search_by_fulltext(["needle"], user_name="t", tsquery_config="english")


def test_fulltext_search_return_fields_are_sanitized_like_the_baseline():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            _fulltext_row(
                MEMORY_ID,
                memory="body text",
                user_name="tenant-1",
                status="activated",
                metadata_json='{"importance": 5}',
            )
        ]
    )

    results = backend.search_by_fulltext(
        ["needle"],
        user_name="tenant-1",
        return_fields=["memory", "user_name", "importance", "bad field; DROP"],
    )

    assert results == [
        {
            "id": MEMORY_ID,
            "score": 1.0,
            "memory": "body text",
            "user_name": "tenant-1",
            "importance": 5,
        }
    ]


def test_add_node_writes_every_vector_filter_field():
    backend = _backend_with_vector_index()

    backend.add_node(
        MEMORY_ID,
        "remember this",
        {
            "embedding": [0.1, 0.2],
            "memory_type": "LongTermMemory",
            "status": "activated",
            "mem_cube_id": "cube-1",
            "vector_sync": "success",
        },
    )

    vector_item = backend._vector_index.upsert.call_args.args[0][0]
    assert str(vector_item.id) == MEMORY_ID
    assert vector_item.payload == {
        "user_name": "tenant-1",
        "memory_type": "LongTermMemory",
        "status": "activated",
        "mem_cube_id": "cube-1",
        "vector_sync": "success",
    }


def test_add_nodes_batch_writes_every_vector_filter_field():
    backend = _backend_with_vector_index()

    backend.add_nodes_batch(
        [
            {
                "id": MEMORY_ID,
                "memory": "remember this",
                "metadata": {
                    "embedding": [0.1, 0.2],
                    "memory_type": "WorkingMemory",
                    "status": "activated",
                    "mem_cube_id": "cube-1",
                },
            }
        ]
    )

    vector_item = backend._vector_index.upsert.call_args.args[0][0]
    assert vector_item.payload == {
        "user_name": "tenant-1",
        "memory_type": "WorkingMemory",
        "status": "activated",
        "mem_cube_id": "cube-1",
        "vector_sync": "success",
    }
    backend._g.addVertex.assert_called_once()
    label, properties = backend._g.addVertex.call_args.args
    assert label == "Memory"
    assert properties["id"] == MEMORY_ID
    assert backend._g.addVertex.call_args.kwargs == {"id": MEMORY_ID}


def test_add_nodes_batch_probes_existence_through_rest_not_gremlin():
    """The gremlin hasId(within(...)) probe resurrected recently deleted ids
    for the replica-lag window (live finding ⑥) and would misroute
    re-imports of those ids to appendVertex. The REST batch read is the
    same authority the single-node lookup trusts."""
    backend = _backend_with_vector_index()
    other_id = "22222222-2222-4222-8222-222222222222"
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(properties={"id": MEMORY_ID, "user_name": "tenant-1", "memory": "old"})
    ]

    backend.add_nodes_batch(
        [
            {"id": MEMORY_ID, "memory": "updated", "metadata": {}},
            {"id": other_id, "memory": "fresh", "metadata": {}},
        ]
    )

    # appendVertex receives the full schema-backed property set; the
    # contract here is routing (existing id → append, new id → add).
    assert backend._g.appendVertex.call_args.args[0] == MEMORY_ID
    assert backend._g.appendVertex.call_args.args[1]["memory"] == "updated"
    label, properties = backend._g.addVertex.call_args.args
    assert label == "Memory"
    assert properties["id"] == other_id
    # The gremlin probe channel must stay out of existence decisions.
    backend._gremlin.exec.assert_not_called()


def test_add_nodes_batch_ignores_foreign_tenant_vertices_in_rest_probe():
    """A same-id vertex owned by another tenant must NOT count as existing —
    the re-import goes through addVertex under the caller's tenant."""
    backend = _backend_with_vector_index()
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(
            properties={"id": MEMORY_ID, "user_name": "someone-else", "memory": "foreign"}
        )
    ]

    backend.add_nodes_batch([{"id": MEMORY_ID, "memory": "mine", "metadata": {}}])

    backend._g.appendVertex.assert_not_called()
    label, properties = backend._g.addVertex.call_args.args
    assert label == "Memory"
    assert properties["id"] == MEMORY_ID


def test_add_nodes_batch_chunks_the_rest_probe_over_100_ids():
    """The REST read rides the query string and the server rejects request
    lines past ~8KB — a 250-id import must be probed in 3 chunks."""
    backend = _backend_with_vector_index()
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(1, 251)]
    backend._g.getVerticesById.return_value = []

    backend.add_nodes_batch([{"id": i, "memory": "bulk", "metadata": {}} for i in ids])

    assert backend._g.getVerticesById.call_count == 3
    assert [len(c.args[0]) for c in backend._g.getVerticesById.call_args_list] == [100, 100, 50]


def test_add_nodes_batch_falls_back_to_chunked_gremlin_probe_when_rest_errors():
    """REST failure must degrade to the gremlin probe, also chunked — the
    engine caps Groovy calls at 254 parameters (live-probed), so 600 ids
    go out as 250/250/100."""
    backend = _backend_with_vector_index()
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(1, 601)]
    backend._g.getVerticesById = MagicMock(side_effect=RuntimeError("REST down"))
    backend._gremlin.exec.return_value = []

    backend.add_nodes_batch([{"id": i, "memory": "bulk", "metadata": {}} for i in ids])

    assert backend._gremlin.exec.call_count == 3  # 250/250/100 groovy chunks
    first_query = backend._gremlin.exec.call_args_list[0].args[0]
    assert "within(*[" in first_query
    assert ".has('user_name'," in first_query  # tenant-scoped fallback probe


def test_delete_node_by_mem_cube_id_chunks_the_status_update_over_300_ids():
    """The engine caps Groovy parameter counts at 254 (live-probed); a cube
    with 300 memories must update status in 250/50 chunks, not one 400ing
    call."""
    backend = _backend_with_vector_index()
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(1, 301)]
    rows = [{"id": [i], "user_name": ["tenant-1"], "status": ["activated"]} for i in ids]
    backend._gremlin.exec.side_effect = [rows] + [[] for _ in range(3)]

    count = backend.delete_node_by_mem_cube_id("tenant-1", delete_record_id="rec-1")

    assert count == 300
    property_calls = [
        c.args[0] for c in backend._gremlin.exec.call_args_list if "property('status'" in c.args[0]
    ]
    assert len(property_calls) == 2  # 250/50 chunks under the 250 cap


def test_list_user_names_returns_sorted_distinct_nonempty_like_the_baseline():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = ["tenant-b", "tenant-a", "", None, "tenant-b"]

    assert backend.list_user_names() == ["tenant-a", "tenant-b"]


def test_add_edge_uses_fixed_schema_label_and_relation_property():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(return_value={"id": MEMORY_ID})

    backend.add_edge(MEMORY_ID, "22222222-2222-4222-8222-222222222222", "PARENT")

    backend._g.addEdge.assert_called_once_with(
        "MEMORY_RELATION",
        MEMORY_ID,
        "22222222-2222-4222-8222-222222222222",
        {"relation_type": "PARENT", "user_name": "tenant-1"},
    )


def test_add_edge_rejects_endpoint_outside_tenant():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(side_effect=[{"id": MEMORY_ID}, None])

    backend.add_edge(MEMORY_ID, "22222222-2222-4222-8222-222222222222", "PARENT")

    backend._g.addEdge.assert_not_called()


def test_k_neighbor_uses_public_pyhugegraph_traverser_api():
    backend = _backend_with_vector_index()
    backend._traverser.k_neighbor.return_value = {
        "size": 1,
        "kneighbor": ["neighbor-1"],
    }
    backend._gremlin.exec.return_value = ["neighbor-1"]

    result = backend.search_by_kneighbor(
        MEMORY_ID,
        max_depth=2,
        limit=7,
        user_name="tenant-1",
        status="activated",
    )

    assert result["neighbors"] == ["neighbor-1"]
    assert result["paths"] == []
    assert result["edges"] == []
    backend._traverser.k_neighbor.assert_called_once_with(MEMORY_ID, 2)
    filter_query = backend._gremlin.exec.call_args.args[0]
    assert '.hasId(within(*["neighbor-1"]))' in filter_query
    assert ".has('user_name', \"tenant-1\")" in filter_query
    assert ".has('status', \"activated\")" in filter_query


def test_neighbors_by_tag_scans_pages_and_excludes_non_long_term_nodes():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            {
                "id": ["working"],
                "memory": ["working memory"],
                "user_name": ["tenant-1"],
                "memory_type": ["WorkingMemory"],
                "status": ["activated"],
                "metadata_json": ['{"tags":["topic"]}'],
            },
            {
                "id": ["reasoning"],
                "memory": ["reasoning memory"],
                "user_name": ["tenant-1"],
                "memory_type": ["LongTermMemory"],
                "status": ["activated"],
                "metadata_json": ['{"tags":["topic"],"type":"reasoning"}'],
            },
            {
                "id": ["candidate"],
                "memory": ["candidate memory"],
                "user_name": ["tenant-1"],
                "memory_type": ["LongTermMemory"],
                "status": ["activated"],
                "metadata_json": ['{"tags":["topic","python"]}'],
            },
        ]
    )

    result = backend.get_neighbors_by_tag(
        tags=["topic", "python"], exclude_ids=[], top_k=1, min_overlap=1
    )

    assert [node["id"] for node in result] == ["candidate"]
    query = backend._gremlin_exec.call_args.args[0]
    assert ".range(0, 1000)" in query
    assert ".limit(" not in query


def test_get_neighbors_by_tag_excludes_client_side_without_chunked_not_hasid():
    """G14.7 major 1: chunked .not(hasId(...)) exclusion queries leak an
    excluded id that first appears in another chunk's result — the exclusion
    must stay client-side (baseline WHERE NOT n.id IN $exclude_ids), with a
    single paged scan instead of server-side hasId chunks."""
    backend = _backend_with_vector_index()
    excluded_id = "ex-0260"
    excluded = {
        "id": excluded_id,
        "memory": ["excluded"],
        "user_name": ["tenant-1"],
        "memory_type": ["LongTermMemory"],
        "status": ["activated"],
        "metadata_json": ['{"tags":["topic"]}'],
    }
    keeper = {
        "id": MEMORY_ID,
        "memory": ["candidate"],
        "user_name": ["tenant-1"],
        "memory_type": ["LongTermMemory"],
        "status": ["activated"],
        "metadata_json": ['{"tags":["topic"]}'],
    }
    backend._gremlin.exec.side_effect = [[excluded, keeper]]

    result = backend.get_neighbors_by_tag(
        tags=["topic"],
        exclude_ids=[excluded_id] + [f"ex-{i:04d}" for i in range(300)],
        top_k=5,
        min_overlap=1,
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert "hasId(" not in query  # 排除不再进服务端（含分块形态）
    assert ".range(0, 1000)" in query
    # 排除 id 即便在扫描结果里也必须被客户端剔除
    assert [node["id"] for node in result] == [MEMORY_ID]


def test_delete_node_does_not_mutate_unknown_tenant_node():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(return_value=None)

    backend.delete_node(MEMORY_ID)

    backend._g.removeVertexById.assert_not_called()
    backend._vector_index.delete.assert_not_called()


def test_get_nodes_filters_vertices_by_tenant():
    backend = _backend_with_vector_index()
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(properties={"id": MEMORY_ID, "memory": "mine", "user_name": "tenant-1"}),
        SimpleNamespace(
            properties={
                "id": "22222222-2222-4222-8222-222222222222",
                "memory": "not mine",
                "user_name": "tenant-2",
            }
        ),
    ]

    nodes = backend.get_nodes([MEMORY_ID, "22222222-2222-4222-8222-222222222222"])

    assert [node["id"] for node in nodes] == [MEMORY_ID]


def test_single_node_lookup_reads_the_vertex_rest_first():
    """REST is authoritative in both directions the gremlin point lookup has
    been wrong live: the 1.7 custom-UUID write bug (REST resolves what
    g.V(uuid) cannot) and the 1.8 delete-visibility lag (g.V(id) keeps a
    deleted vertex visible for tens of seconds while REST is correct)."""
    backend = _backend_with_vector_index()
    properties = {
        "id": MEMORY_ID,
        "memory": "stored memory",
        "user_name": "tenant-1",
        "metadata_json": "{}",
    }
    backend._g.getVerticesById.return_value = [SimpleNamespace(properties=properties)]
    backend._gremlin_exec = MagicMock()

    assert backend._get_vertex_by_node_id(MEMORY_ID)["id"] == MEMORY_ID
    assert backend.get_node(MEMORY_ID)["id"] == MEMORY_ID
    backend._gremlin_exec.assert_not_called()


def test_single_node_lookup_trusts_an_empty_rest_answer_over_gremlin():
    """A normal empty REST answer settles it: falling through to gremlin
    resurrected deleted vertices for the whole replica-lag window (live:
    get_node still returned the node 45s after DELETE 204)."""
    backend = _backend_with_vector_index()
    backend._g.getVerticesById.return_value = []
    backend._gremlin_exec = MagicMock()

    assert backend._get_vertex_by_node_id(MEMORY_ID) is None
    backend._gremlin_exec.assert_not_called()


def test_single_node_lookup_falls_back_to_gremlin_when_rest_errors():
    """Gremlin only covers the case where the REST call itself failed, not
    where it answered."""
    backend = _backend_with_vector_index()
    backend._g.getVerticesById = MagicMock(side_effect=RuntimeError("REST down"))
    backend._gremlin_exec = MagicMock(
        return_value=[{"id": [MEMORY_ID], "user_name": ["tenant-1"], "memory": ["m"]}]
    )

    assert backend._get_vertex_by_node_id(MEMORY_ID)["id"] == MEMORY_ID
    assert backend._gremlin_exec.call_args.args[0].startswith(f'g.V("{MEMORY_ID}")')


def test_grouped_counts_names_single_group_field_like_neo4j():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[{"LongTermMemory": 3, "WorkingMemory": 1}])

    assert backend.get_grouped_counts(["memory_type"]) == [
        {"memory_type": "LongTermMemory", "count": 3},
        {"memory_type": "WorkingMemory", "count": 1},
    ]


def test_grouped_counts_supports_multiple_fields():
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(
        return_value=[
            {"memory_type": ["LongTermMemory"], "status": ["activated"]},
            {"memory_type": ["LongTermMemory"], "status": ["activated"]},
            {"memory_type": ["WorkingMemory"], "status": ["archived"]},
        ]
    )

    assert backend.get_grouped_counts(["memory_type", "status"]) == [
        {"memory_type": "LongTermMemory", "status": "activated", "count": 2},
        {"memory_type": "WorkingMemory", "status": "archived", "count": 1},
    ]


def test_update_embedding_upserts_vector_with_updated_filters():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(
        return_value={
            "id": MEMORY_ID,
            "memory_type": "LongTermMemory",
            "status": "activated",
            "mem_cube_id": "cube-1",
            "metadata_json": "{}",
        }
    )

    backend.update_node(MEMORY_ID, {"embedding": [0.3, 0.4], "status": "archived"})

    vector_item = backend._vector_index.upsert.call_args.args[0][0]
    assert vector_item.payload == {
        "user_name": "tenant-1",
        "memory_type": "LongTermMemory",
        "status": "archived",
        "mem_cube_id": "cube-1",
        "vector_sync": "success",
    }


# ---------------------------------------------------------------------------
# export_graph parameter parity (G8, baseline = neo4j.py:1298): pagination on
# nodes AND edges with pre-pagination totals, status=None excludes 'deleted'
# (via not(), the engine rejects neq() on non-indexed properties), explicit
# status list = within, and the and/or filter dict applies to nodes and both
# edge endpoints.
# ---------------------------------------------------------------------------


def _export_row(node_id: str, created: str, **props) -> dict:
    row = {"id": [node_id], "memory": [props.pop("memory", "body")], "created_at": [created]}
    for key, value in props.items():
        row[key] = [value]
    return row


def _stub_export_queries(backend, node_rows, edge_rows, total_nodes, total_edges):
    """无 filter 导出共四条查询：count_nodes / count_edges / node 页 / edge 页，
    按查询内容分发返回值。"""

    def dispatch(query):
        if query.endswith(".count()"):
            if "MEMORY_RELATION" in query:
                return [total_edges]
            return [total_nodes]
        if "outE(" in query:
            return edge_rows
        return node_rows

    backend._gremlin_exec = MagicMock(side_effect=dispatch)


def test_export_graph_excludes_deleted_by_default_like_the_baseline():
    backend = _backend_with_vector_index()
    _stub_export_queries(
        backend,
        [_export_row(MEMORY_ID, "2025-01-01T00:00:00", user_name="tenant-1")],
        [],
        total_nodes=1,
        total_edges=0,
    )

    backend.export_graph(user_name="tenant-1")

    query = backend._gremlin_exec.call_args_list[0].args[0]  # 节点计数查询
    assert ".not(__.has('status', 'deleted'))" in query

    backend.export_graph(user_name="tenant-1", status=["activated", "archived"])
    query = backend._gremlin_exec.call_args_list[4].args[0]  # 第二次导出的节点计数
    assert '.has(\'status\', within("activated", "archived"))' in query

    backend.export_graph(user_name="tenant-1", status=[])
    query = backend._gremlin_exec.call_args_list[8].args[0]
    assert "status" not in query  # explicit empty list imposes no constraint


def test_export_graph_honours_memory_type_and_paginates_with_pre_pagination_totals():
    backend = _backend_with_vector_index()
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(11, 18)]
    # page 3 at size 2 → 服务端 .range(4, 6)，stub 只需返回该页两行
    _stub_export_queries(
        backend,
        [_export_row(i, f"2025-01-0{i[-1]}T00:00:00", user_name="tenant-1") for i in ids[4:6]],
        [],
        total_nodes=7,
        total_edges=0,
    )

    exported = backend.export_graph(
        user_name="tenant-1", memory_type=["WorkingMemory"], page=3, page_size=2
    )

    count_query = backend._gremlin_exec.call_args_list[0].args[0]
    assert ".has('memory_type', within(\"WorkingMemory\"))" in count_query
    node_query = backend._gremlin_exec.call_args_list[2].args[0]
    assert ".order().by('created_at', desc).by('id', desc).range(4, 6).valueMap(true)" in node_query
    assert len(exported["nodes"]) == 2  # page 3 of 7 items at size 2 → items 5–6
    assert exported["total_nodes"] == 7  # counted before pagination
    assert exported["total_edges"] == 0


def test_export_graph_applies_the_filter_to_nodes_and_both_edge_endpoints():
    backend = _backend_with_vector_index()
    other_id = "22222222-2222-4222-8222-222222222222"
    node_rows = [
        _export_row(MEMORY_ID, "2025-01-02T00:00:00", user_name="t", memory_type="LongTermMemory"),
        _export_row(other_id, "2025-01-01T00:00:00", user_name="t", memory_type="WorkingMemory"),
    ]
    edge_rows = [
        {
            "source": MEMORY_ID,
            "target": other_id,
            "type": "PARENT",
            "a_created": "2025-01-02T00:00:00",
            "b_created": "2025-01-01T00:00:00",
        }
    ]
    # 三次导出，每次发节点+边两条查询
    backend._gremlin_exec = MagicMock(
        side_effect=[node_rows, edge_rows, node_rows, edge_rows, node_rows, edge_rows]
    )

    exported = backend.export_graph(user_name="t", filter={"memory_type": "LongTermMemory"})

    assert [n["id"] for n in exported["nodes"]] == [MEMORY_ID]
    assert exported["edges"] == []  # target endpoint fails the filter
    assert exported["total_edges"] == 0

    # or-branch keeps the edge when either endpoint matches
    exported = backend.export_graph(
        user_name="t",
        filter={"or": [{"memory_type": "LongTermMemory"}, {"memory_type": "WorkingMemory"}]},
    )
    assert exported["edges"] == [{"source": MEMORY_ID, "target": other_id, "type": "PARENT"}]
    assert exported["total_edges"] == 1


def test_export_graph_clamps_pagination_like_the_baseline():
    backend = _backend_with_vector_index()
    ids = [str(__import__("uuid").UUID(int=i)) for i in range(11, 16)]
    # page=0 夹取为 1 → 服务端 .range(0, 3)，stub 返回第一页三行
    _stub_export_queries(
        backend,
        [_export_row(i, f"2025-01-0{i[-1]}T00:00:00", user_name="t") for i in ids[:3]],
        [],
        total_nodes=5,
        total_edges=0,
    )

    exported = backend.export_graph(user_name="t", page=0, page_size=3)

    node_query = backend._gremlin_exec.call_args_list[2].args[0]
    assert ".range(0, 3)" in node_query  # page clamped to 1, size 3 kept
    assert len(exported["nodes"]) == 3
    assert exported["total_nodes"] == 5


# ---------------------------------------------------------------------------
# Structure-maintenance parity (G10): neo4j.py:1248/1255/1263 raises
# NotImplementedError for these three methods with zero call sites, so the
# HugeGraph backend must match — implementing them here would DIVERGE.
# ---------------------------------------------------------------------------


def test_structure_optimization_candidates_counts_only_parent_edges():
    """The baseline looks for the absence of PARENT edges specifically; on
    this backend the edge type rides on relation_type (D6), so a node with
    only a SUPPORTS edge stays a candidate."""
    backend = _backend_with_vector_index()
    backend._gremlin_exec = MagicMock(return_value=[])

    backend.get_structure_optimization_candidates("WorkingMemory", user_name="tenant-1")

    query = backend._gremlin_exec.call_args.args[0]
    assert ".has('status', 'activated')" in query
    assert ".has('relation_type', 'PARENT')" in query
    assert ".filter(bothE('MEMORY_RELATION')" in query


def test_structure_maintenance_methods_match_the_baseline_stub():
    import inspect

    from memos.graph_dbs.neo4j import Neo4jGraphDB

    backend = _backend_without_vector_index()
    with pytest.raises(NotImplementedError):
        backend.deduplicate_nodes()
    with pytest.raises(NotImplementedError):
        backend.detect_conflicts()
    with pytest.raises(NotImplementedError):
        backend.merge_nodes("a", "b")

    for name in ("deduplicate_nodes", "detect_conflicts", "merge_nodes"):
        baseline_source = inspect.getsource(getattr(Neo4jGraphDB, name))
        assert "raise NotImplementedError" in baseline_source, (
            f"Neo4jGraphDB.{name} changed; revisit the HugeGraph stub for parity"
        )


def test_exported_edges_can_be_imported_without_translation():
    backend = _backend_with_vector_index()
    _stub_export_queries(
        backend,
        [
            _export_row(MEMORY_ID, "2025-01-01T00:00:00", user_name="tenant-1"),
            _export_row(
                "22222222-2222-4222-8222-222222222222", "2025-01-01T00:00:00", user_name="tenant-1"
            ),
        ],
        [
            {
                "source": MEMORY_ID,
                "target": "22222222-2222-4222-8222-222222222222",
                "type": "PARENT",
                "a_created": "2025-01-01T00:00:00",
                "b_created": "2025-01-01T00:00:00",
            }
        ],
        total_nodes=2,
        total_edges=1,
    )
    exported = backend.export_graph()
    backend.add_nodes_batch = MagicMock()
    backend.add_edge = MagicMock()

    backend.import_graph(exported)

    backend.add_edge.assert_called_once_with(
        MEMORY_ID,
        "22222222-2222-4222-8222-222222222222",
        "PARENT",
        None,
    )


def test_import_graph_accepts_legacy_from_to_edge_shape():
    backend = _backend_with_vector_index()
    backend.add_edge = MagicMock()

    backend.import_graph(
        {
            "edges": [
                {
                    "from": MEMORY_ID,
                    "to": "22222222-2222-4222-8222-222222222222",
                    "type": "PARENT",
                }
            ]
        }
    )

    backend.add_edge.assert_called_once_with(
        MEMORY_ID,
        "22222222-2222-4222-8222-222222222222",
        "PARENT",
        None,
    )


def test_get_node_encodes_untrusted_id_as_one_gremlin_literal():
    backend = _backend_with_vector_index()
    backend._g.getVerticesById = MagicMock(side_effect=RuntimeError("REST down"))
    backend._gremlin.exec.return_value = []
    hostile_id = "memory').drop(); g.V('"

    backend.get_node(hostile_id)

    query = backend._gremlin.exec.call_args.args[0]
    assert query.startswith(f"g.V({backend._gremlin_literal(hostile_id)})")
    assert query.count(".drop()") == 1
    assert f"has('user_name', {backend._gremlin_literal('tenant-1')})" in query
    backend._g.getVerticesById.assert_called_once_with([hostile_id])


def test_update_filter_fields_refreshes_vector_payload_without_new_embedding():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(
        return_value={
            "id": MEMORY_ID,
            "memory_type": "LongTermMemory",
            "status": "activated",
            "mem_cube_id": "cube-1",
            "embedding_json": "[0.1, 0.2]",
            "metadata_json": "{}",
        }
    )

    backend.update_node(MEMORY_ID, {"status": "archived"})

    vector_item = backend._vector_index.upsert.call_args.args[0][0]
    assert vector_item.vector == [0.1, 0.2]
    assert vector_item.payload["status"] == "archived"


def test_delete_node_surfaces_vector_failure_before_graph_delete():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(return_value={"id": MEMORY_ID})
    backend._vector_index.delete.side_effect = RuntimeError("vector unavailable")

    with pytest.raises(RuntimeError, match="vector unavailable"):
        backend.delete_node(MEMORY_ID)

    backend._g.removeVertexById.assert_not_called()


def test_clear_deletes_tenant_vectors_before_graph_vertices():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [[MEMORY_ID], []]

    backend.clear()

    backend._vector_index.delete.assert_called_once_with([MEMORY_ID])
    queries = [call.args[0] for call in backend._gremlin.exec.call_args_list]
    assert queries[0].endswith(".values('id')")
    assert queries[1].endswith(".drop()")


def test_remove_oldest_memory_deletes_matching_vectors():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [[MEMORY_ID], []]

    backend.remove_oldest_memory("WorkingMemory", keep_latest=10)

    backend._vector_index.delete.assert_called_once_with([MEMORY_ID])
    queries = [call.args[0] for call in backend._gremlin.exec.call_args_list]
    assert ".range(10, -1).values('id')" in queries[0]
    assert ".range(10, -1).drop()" in queries[1]


def test_delete_by_mem_cube_id_deletes_matching_vectors():
    """Hard delete: the server-side scan narrows by indexed user_name only
    (delete_record_id equality is rejected by this engine), the record id
    filter runs client-side, and the drop goes out chunked."""
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [
        [
            {
                "id": [MEMORY_ID],
                "user_name": ["tenant-1"],
                "delete_record_id": ["record-1"],
            }
        ],
        [],
    ]

    backend.delete_node_by_mem_cube_id(
        mem_cube_id="tenant-1", delete_record_id="record-1", hard_delete=True
    )

    backend._vector_index.delete.assert_called_once_with([MEMORY_ID])
    scan_query = backend._gremlin.exec.call_args_list[0].args[0]
    # the record id must never become a server-side condition
    assert "has('delete_record_id'" not in scan_query
    assert ".project('id', 'delete_record_id')" in scan_query
    drop_query = backend._gremlin.exec.call_args_list[1].args[0]
    assert drop_query.startswith("g.V().hasId(within(*[")


def test_recover_by_mem_cube_id_refreshes_vector_status():
    backend = _backend_with_vector_index()
    full_row = {
        "id": [MEMORY_ID],
        "memory_type": ["LongTermMemory"],
        "status": ["archived"],
        "mem_cube_id": ["cube-1"],
        "embedding_json": ["[0.1, 0.2]"],
        "delete_record_id": ["record-1"],
    }
    backend._gremlin.exec.side_effect = [
        [full_row],  # projected scan (client filters on delete_record_id)
        [full_row],  # full-property refetch for the vector sync
        [],  # chunked status update
    ]

    backend.recover_memory_by_mem_cube_id("tenant-1", "record-1")

    vector_item = backend._vector_index.upsert.call_args.args[0][0]
    assert vector_item.vector == [0.1, 0.2]
    assert vector_item.payload["status"] == "activated"


def test_get_by_metadata_filters_json_metadata_with_and_semantics():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "memory_type": ["LongTermMemory"],
            "metadata_json": ['{"key":"alpha","tags":["one","two"]}'],
        },
        {
            "id": second_id,
            "memory_type": ["LongTermMemory"],
            "metadata_json": ['{"key":"beta","tags":["three"]}'],
        },
    ]

    ids = backend.get_by_metadata(
        [
            {"field": "key", "op": "in", "value": ["alpha"]},
            {"field": "tags", "op": "contains", "value": ["two", "missing"]},
            {"field": "memory_type", "op": "=", "value": "LongTermMemory"},
        ]
    )

    assert ids == [MEMORY_ID]
    assert backend._gremlin.exec.call_args.args[0].endswith(".valueMap(true)")


def test_get_by_metadata_rejects_unknown_operator():
    backend = _backend_with_vector_index()

    with pytest.raises(ValueError, match="Unsupported metadata operator"):
        backend.get_by_metadata([{"field": "key", "op": "exec", "value": "x"}])


# ---------------------------------------------------------------------------
# get_by_metadata pushdown and parameter parity (G6): schema-key predicates
# narrow the Gremlin scan server-side (live-probed bindings: within(), P.gt
# family, Text.contains; this HStore build has no prefix/suffix predicate);
# metadata_json fields stay client-side; knowledgebase_ids OR into the user
# clause; the and/or filter dict post-filters like the baseline.
# ---------------------------------------------------------------------------


def test_get_by_metadata_pushes_in_and_range_predicates_into_gremlin():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = []

    backend.get_by_metadata(
        [
            {"field": "memory_type", "op": "in", "value": ["LongTermMemory", "WorkingMemory"]},
            {"field": "created_at", "op": ">=", "value": "2025-01-01T00:00:00"},
        ],
        user_name="tenant-1",
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert '.has(\'memory_type\', within("LongTermMemory", "WorkingMemory"))' in query
    assert ".has('created_at', P.gte(\"2025-01-01T00:00:00\"))" in query


def test_get_by_metadata_chunks_oversized_in_pushdown_under_the_index_condition_cap():
    """Live-probed second cap: indexed within() conditions die at 101 with
    "Too many conditions(101) each query", so a 251-value `in` runs as three
    queries (100/100/51) with the rows merged and deduplicated client-side."""
    backend = _backend_with_vector_index()
    other_id = "22222222-2222-4222-8222-222222222222"
    dup = {"id": MEMORY_ID, "user_name": ["tenant-1"], "memory_type": ["LongTermMemory"]}
    other = {"id": other_id, "user_name": ["tenant-1"], "memory_type": ["LongTermMemory"]}
    backend._gremlin.exec.side_effect = [[dup], [dup, other], [dup]]

    values = [f"mt-{i:04d}" for i in range(250)] + ["LongTermMemory"]
    ids = backend.get_by_metadata(
        [{"field": "memory_type", "op": "in", "value": values}], user_name="tenant-1"
    )

    calls = backend._gremlin.exec.call_args_list
    assert len(calls) == 3
    assert calls[0].args[0].count("mt-") == 100
    assert calls[1].args[0].count("mt-") == 100
    assert calls[2].args[0].count("mt-") == 50
    assert '"LongTermMemory"' in calls[2].args[0]
    for call in calls:
        assert call.args[0].count("within(") == 1
        assert call.args[0].endswith(".valueMap(true)")
    # 同一行命中多个分块查询时只保留一份
    assert ids == [MEMORY_ID, other_id]


def test_get_by_metadata_pushes_contains_for_schema_keys_only():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {
            "id": second_id,
            "memory": ["订单发货延迟"],
            "user_name": ["tenant-1"],
            "metadata_json": ['{"tags":["vip","early"]}'],
        },
    ]

    ids = backend.get_by_metadata(
        [
            {"field": "memory", "op": "contains", "value": "订单"},
            {"field": "tags", "op": "contains", "value": "vip"},
        ],
        user_name="tenant-1",
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert ".has('memory', Text.contains(\"订单\"))" in query
    assert "tags" not in query  # metadata_json 字段没有可索引属性，留在客户端
    assert ids == [second_id]


def test_get_by_metadata_starts_with_stays_client_side_without_server_support():
    """Live probe: this HStore build exposes no Text prefix/suffix predicate."""
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {"id": MEMORY_ID, "user_name": ["tenant-1"], "metadata_json": ['{"code":"ERR-001"}']},
        {"id": second_id, "user_name": ["tenant-1"], "metadata_json": ['{"code":"OK-1"}']},
    ]

    ids = backend.get_by_metadata(
        [{"field": "code", "op": "starts_with", "value": "ERR"}], user_name="tenant-1"
    )

    assert "Text" not in backend._gremlin.exec.call_args.args[0]
    assert ids == [MEMORY_ID]


def test_get_by_metadata_honours_knowledgebase_ids_as_extra_user_names():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {"id": MEMORY_ID, "user_name": ["tenant-1"], "memory_type": ["LongTermMemory"]},
        {"id": second_id, "user_name": ["kb-7"], "memory_type": ["LongTermMemory"]},
    ]

    ids = backend.get_by_metadata(
        [{"field": "memory_type", "op": "=", "value": "LongTermMemory"}],
        user_name="tenant-1",
        knowledgebase_ids=["kb-7"],
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert ".or(" in query and "kb-7" in query and ".dedup()" in query
    assert sorted(ids) == sorted([MEMORY_ID, second_id])


def test_get_by_metadata_applies_the_and_or_filter_dict():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "user_name": ["tenant-1"],
            "memory_type": ["LongTermMemory"],
            "metadata_json": ['{"importance": 5}'],
        },
        {
            "id": second_id,
            "user_name": ["tenant-1"],
            "memory_type": ["WorkingMemory"],
            "metadata_json": ['{"importance": 1}'],
        },
    ]

    ids = backend.get_by_metadata(
        [],
        user_name="tenant-1",
        filter={"and": [{"memory_type": "LongTermMemory"}, {"importance": {"gte": 3}}]},
    )
    assert ids == [MEMORY_ID]

    ids = backend.get_by_metadata(
        [],
        user_name="tenant-1",
        filter={"or": [{"memory_type": "WorkingMemory"}, {"importance": {"gte": 3}}]},
    )
    assert sorted(ids) == sorted([MEMORY_ID, second_id])


def test_get_by_metadata_in_empty_value_returns_without_scanning():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = []

    assert backend.get_by_metadata([{"field": "key", "op": "in", "value": []}]) == []
    backend._gremlin.exec.assert_not_called()


def test_get_by_metadata_normalizes_datetime_values_to_iso_strings():
    from datetime import datetime

    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = [
        {"id": MEMORY_ID, "user_name": ["tenant-1"], "created_at": ["2025-06-01T00:00:00"]}
    ]

    ids = backend.get_by_metadata(
        [{"field": "created_at", "op": ">=", "value": datetime(2025, 1, 1)}],
        user_name="tenant-1",
    )

    assert (
        ".has('created_at', P.gte(\"2025-01-01T00:00:00\"))"
        in (backend._gremlin.exec.call_args.args[0])
    )
    assert ids == [MEMORY_ID]


def test_get_by_metadata_accepts_user_name_flag_like_the_dead_baseline_param():
    """The baseline declares user_name_flag and never reads it; parity means
    accepting it without behaviour change."""
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = []

    backend.get_by_metadata([], user_name="tenant-1", user_name_flag=False)
    backend._gremlin.exec.assert_called_once()


# ---------------------------------------------------------------------------
# get_all_memory_items parameter parity (G7): knowledgebase_ids OR into the
# user clause, the and/or filter dict post-filters, scope validation matches
# the baseline. status is a recorded divergence: the baseline silently drops
# the caller's status kwarg, this backend honours it.
# ---------------------------------------------------------------------------


def test_get_all_memory_items_honours_knowledgebase_ids_as_extra_user_names():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {"id": MEMORY_ID, "user_name": ["tenant-1"], "memory_type": ["WorkingMemory"]},
        {"id": second_id, "user_name": ["kb-7"], "memory_type": ["WorkingMemory"]},
    ]

    items = backend.get_all_memory_items(
        "WorkingMemory", user_name="tenant-1", knowledgebase_ids=["kb-7"]
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert ".or(" in query and "kb-7" in query and ".dedup()" in query
    assert [i["id"] for i in items] == [MEMORY_ID, second_id]


def test_get_all_memory_items_applies_the_and_or_filter_dict():
    backend = _backend_with_vector_index()
    second_id = "22222222-2222-4222-8222-222222222222"
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "user_name": ["tenant-1"],
            "memory_type": ["WorkingMemory"],
            "metadata_json": ['{"importance": 5}'],
        },
        {
            "id": second_id,
            "user_name": ["tenant-1"],
            "memory_type": ["WorkingMemory"],
            "metadata_json": ['{"importance": 1}'],
        },
    ]

    items = backend.get_all_memory_items(
        "WorkingMemory",
        user_name="tenant-1",
        filter={"and": [{"importance": {"gte": 3}}]},
    )
    assert [i["id"] for i in items] == [MEMORY_ID]

    items = backend.get_all_memory_items(
        "WorkingMemory",
        user_name="tenant-1",
        filter={"or": [{"importance": {"gte": 3}}, {"importance": {"lt": 3}}]},
    )
    assert {i["id"] for i in items} == {MEMORY_ID, second_id}


def test_get_all_memory_items_validates_scope_like_the_baseline():
    backend = _backend_with_vector_index()

    with pytest.raises(ValueError, match="Unsupported memory type scope"):
        backend.get_all_memory_items("BogusScope")
    backend._gremlin.exec.assert_not_called()


def test_get_all_memory_items_honours_status_and_include_embedding():
    backend = _backend_with_vector_index()
    embedding = [0.1, 0.2, 0.3]
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "user_name": ["tenant-1"],
            "memory_type": ["WorkingMemory"],
            "status": ["activated"],
            "embedding_json": ["[0.1, 0.2, 0.3]"],
        },
    ]

    items = backend.get_all_memory_items(
        "WorkingMemory", include_embedding=True, user_name="tenant-1", status="activated"
    )

    query = backend._gremlin.exec.call_args.args[0]
    assert ".has('memory_type', \"WorkingMemory\")" in query
    assert ".has('status', \"activated\")" in query
    assert items[0]["metadata"]["embedding"] == embedding


def test_embedding_search_hydrates_fields_with_requested_tenant():
    """cube_name must drive the graph-side hydration too — otherwise the
    vector hits all come back with missing fields (REST lookup filters by
    the tenant, like the real get_nodes does)."""
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [SimpleNamespace(id=MEMORY_ID, score=0.9)]
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(properties={"id": MEMORY_ID, "memory": "mine", "user_name": "kb-9"}),
        SimpleNamespace(
            properties={
                "id": "33333333-3333-4333-8333-333333333333",
                "memory": "someone-elses",
                "user_name": "someone-else",
            }
        ),
    ]

    results = backend.search_by_embedding([0.1, 0.2], return_fields=["memory"], cube_name="kb-9")

    assert len(results) == 1
    assert results[0]["id"] == MEMORY_ID
    assert results[0]["memory"] == "mine"


def test_get_nodes_honors_include_embedding():
    backend = _backend_with_vector_index()
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(
            properties={
                "id": MEMORY_ID,
                "memory": "mine",
                "user_name": "tenant-1",
                "embedding_json": "[0.1, 0.2]",
            }
        )
    ]

    nodes = backend.get_nodes([MEMORY_ID], include_embedding=True)

    assert nodes[0]["metadata"]["embedding"] == [0.1, 0.2]


def test_soft_delete_and_recover_support_server_router_signature():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [
        [
            {
                "id": MEMORY_ID,
                "status": ["activated"],
                "memory_type": ["LongTermMemory"],
                "mem_cube_id": ["cube-1"],
                "embedding_json": ["[0.1, 0.2]"],
            }
        ],
        [],
        [
            {
                "id": MEMORY_ID,
                "status": ["deleted"],
                "memory_type": ["LongTermMemory"],
                "mem_cube_id": ["cube-1"],
                "embedding_json": ["[0.1, 0.2]"],
                "delete_record_id": ["record-1"],
            }
        ],
        [
            {
                "id": MEMORY_ID,
                "status": ["deleted"],
                "memory_type": ["LongTermMemory"],
                "mem_cube_id": ["cube-1"],
                "embedding_json": ["[0.1, 0.2]"],
                "delete_record_id": ["record-1"],
            }
        ],
        [],
    ]

    deleted = backend.delete_node_by_mem_cube_id(
        mem_cube_id="tenant-1", delete_record_id="record-1", hard_delete=False
    )
    recovered = backend.recover_memory_by_mem_cube_id(
        mem_cube_id="tenant-1", delete_record_id="record-1"
    )

    assert deleted == 1
    assert recovered == 1
    vector_items = [call.args[0][0] for call in backend._vector_index.upsert.call_args_list]
    assert [item.payload["status"] for item in vector_items] == ["deleted", "activated"]


def test_delete_node_by_prams_supports_memory_ids_contract():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [
        [{"id": MEMORY_ID, "user_name": ["tenant-1"], "metadata_json": ["{}"]}],
        [],
    ]

    count = backend.delete_node_by_prams(memory_ids=[MEMORY_ID])

    assert count == 1
    backend._vector_index.delete.assert_called_once_with([MEMORY_ID])
    # Baseline: no writable_cube_ids means a global delete by id — no tenant
    # clause is injected, and tree.py:419 (memory_ids only) stops silently
    # deleting 0 rows.
    first_query = backend._gremlin.exec.call_args_list[0].args[0]
    assert "user_name" not in first_query


def test_add_node_retry_repairs_vector_after_graph_partial_write():
    backend = _backend_with_vector_index()
    backend._get_vertex_by_node_id = MagicMock(return_value={"id": MEMORY_ID})

    backend.add_node(
        MEMORY_ID,
        "remember this",
        {"embedding": [0.1, 0.2], "status": "activated"},
    )

    backend._g.addVertex.assert_not_called()
    backend._g.appendVertex.assert_called_once()
    backend._vector_index.upsert.assert_called_once()


def test_delete_node_by_prams_supports_cube_file_and_nested_filter_contract():
    backend = _backend_with_vector_index()
    row = {
        "id": MEMORY_ID,
        "user_name": ["cube-a"],
        "metadata_json": ['{"file_ids":["file-1"],"user_id":"user-1","tags":["keep"]}'],
    }
    # 三次查询：filter 先经 get_by_metadata 解析，再 probe，再 drop
    backend._gremlin.exec.side_effect = [[row], [row], []]

    count = backend.delete_node_by_prams(
        writable_cube_ids=["cube-a"],
        file_ids=["file-1"],
        filter={"and": [{"user_id": "user-1"}, {"tags": {"contains": "keep"}}]},
    )

    assert count == 1
    # probe query: cube scope narrows candidates, but file_ids are an
    # independent OR branch — no hasId narrowing, or file-only matches
    # would be unreachable (silent under-deletion, reproduced live).
    probe_query = backend._gremlin.exec.call_args_list[1].args[0]
    assert 'within("cube-a")' in probe_query
    assert "hasId(within(" not in probe_query


def test_delete_node_by_prams_file_ids_stay_an_independent_or_branch():
    """Baseline neo4j_community.py:1032: (memory_ids) OR (file_ids all
    present) OR (filter ids). A node matching only via file_ids must be
    deleted even when memory_ids are also passed — reproduced live as a
    silent under-deletion during the G14 re-review."""
    backend = _backend_with_vector_index()
    memory_hit = {
        "id": MEMORY_ID,
        "user_name": ["cube-a"],
        "metadata_json": ['{"file_ids":[]}'],
    }
    file_hit_id = "44444444-4444-4444-8444-444444444444"
    file_hit = {
        "id": file_hit_id,
        "user_name": ["cube-a"],
        "metadata_json": ['{"file_ids":["file-1"]}'],
    }
    # probe returns both rows, then drop
    backend._gremlin.exec.side_effect = [[memory_hit, file_hit], []]

    count = backend.delete_node_by_prams(
        writable_cube_ids=["cube-a"], memory_ids=[MEMORY_ID], file_ids=["file-1"]
    )

    assert count == 2
    probe_query = backend._gremlin.exec.call_args_list[0].args[0]
    assert 'within("cube-a")' in probe_query
    assert "hasId(within(" not in probe_query
    backend._vector_index.delete.assert_called_once_with([MEMORY_ID, file_hit_id])


def test_delete_node_by_prams_chunks_cubes_at_the_index_condition_cap():
    """G14.5 major: cube lists feed indexed within() conditions, capped at 100
    per query (tighter than the 250 Groovy parameter cap) — 251 cubes × 251
    memory ids probe as 3 × 2 queries and drop in 2 chunks."""
    backend = _backend_with_vector_index()
    row = {
        "id": "mem-0000",
        "user_name": ["cube-0000"],
        "metadata_json": ['{"file_ids":[]}'],
    }
    backend._gremlin.exec.side_effect = [[row]] + [[]] * 7

    cubes = [f"cube-{i:04d}" for i in range(251)]
    memory_ids = [f"mem-{i:04d}" for i in range(251)]

    count = backend.delete_node_by_prams(writable_cube_ids=cubes, memory_ids=memory_ids)

    assert count == 1
    calls = backend._gremlin.exec.call_args_list
    assert len(calls) == 7  # 6 probe (3 cube chunks × 2 id chunks) + 1 drop
    for i, call in enumerate(calls[:6]):
        probe = call.args[0]
        assert probe.count("within(") == 2  # user_name 条件 + hasId 收窄
        assert probe.count("cube-") == (100 if i < 4 else 51)
        assert probe.count("mem-") == (250 if i % 2 == 0 else 1)
    assert calls[6].args[0] == 'g.V().hasId(within(*["mem-0000"])).drop()'
    backend._vector_index.delete.assert_called_once_with(["mem-0000"])


def test_get_neighbors_by_tag_pins_none_exclude_ids_divergence():
    """Recorded divergence (D3): the baseline's WHERE NOT n.id IN null
    matches nothing; here None violates the list[str] contract and fails
    loudly (G14.9 minor 1)."""
    backend = _backend_with_vector_index()

    with pytest.raises(TypeError):
        backend.get_neighbors_by_tag(tags=["topic"], exclude_ids=None)

    backend._gremlin.exec.assert_not_called()


def test_split_within_clause_branch_counts_match_the_probed_ceiling():
    """G14.9 probed the live ceiling in branch counts (23 pass, 24 die with
    an empty-message ServerError) — pin the value→branch arithmetic so the
    docstring numbers cannot silently drift again."""

    def branches(value_count: int) -> int:
        limit = HugeGraphGraphDB._GREMLIN_INDEX_CONDITION_LIMIT
        return len(HugeGraphGraphDB._chunked(list(range(value_count)), limit))

    assert branches(2100) == 21
    assert branches(2103) == 22
    assert branches(2203) == 23  # 实测通过上限
    assert branches(2303) == 24  # 实测拒绝下限


def test_export_graph_splits_oversized_within_lists_into_or_clauses():
    """The indexed within() cap is per clause (live: 101 → "Too many
    conditions(101) each query"; two within(100) clauses coexist), so an
    oversized memory_type list becomes an or() of disjoint chunks — disjoint
    chunks cannot double-emit a vertex."""
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = []

    backend.export_graph(memory_type=[f"mt-{i:03d}" for i in range(150)])

    query = backend._gremlin.exec.call_args.args[0]
    assert ".or(__.has('memory_type', within(" in query
    assert query.count("mt-") == 300  # conditions 出现两次（起点 + where inV），无值被吞
    first_chunk = query.split("within(")[1]
    assert first_chunk.count("mt-") == 100  # 每个子句 ≤100


def test_delete_node_by_prams_raises_for_non_string_cube_ids():
    """Recorded divergence (D3): the baseline silently matches nothing for a
    non-str cube id; here that input is a caller bug and fails loudly."""
    backend = _backend_with_vector_index()

    with pytest.raises(ValueError, match="writable_cube_ids"):
        backend.delete_node_by_prams(writable_cube_ids=[1, 2], memory_ids=[MEMORY_ID])

    backend._gremlin.exec.assert_not_called()


def test_delete_node_by_prams_returns_zero_without_a_graph_scan_when_nothing_matches():
    """Filter resolved to nothing and no id conditions: the baseline's WHERE
    is empty and deletes 0 — the probe must not scan the whole graph."""
    backend = _backend_with_vector_index()
    backend.get_by_metadata = MagicMock(return_value=[])

    assert (
        backend.delete_node_by_prams(writable_cube_ids=["cube-a"], filter={"user_id": "gone"}) == 0
    )
    backend._gremlin.exec.assert_not_called()


def test_path_traversal_scopes_vertices_and_edges_to_tenant():
    backend = _backend_with_vector_index()
    target_id = "22222222-2222-4222-8222-222222222222"

    backend.get_path(MEMORY_ID, target_id, user_name="tenant-1")

    query = backend._gremlin.exec.call_args.args[0]
    assert query.count("has('user_name', \"tenant-1\")") >= 3


def test_delete_node_by_prams_deletes_globally_by_id_without_cubes_like_the_baseline():
    """neo4j_community has no tenant condition when writable_cube_ids are
    absent; the old behaviour (fall back to the default tenant) silently
    deleted 0 rows for cross-tenant ids."""
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = [
        {"id": MEMORY_ID, "user_name": ["tenant-2"], "metadata_json": ["{}"]}
    ]

    assert backend.delete_node_by_prams(memory_ids=[MEMORY_ID]) == 1
    backend._vector_index.delete.assert_called_once_with([MEMORY_ID])


def test_delete_node_by_prams_requires_cubes_for_file_ids_like_the_baseline():
    backend = _backend_with_vector_index()

    with pytest.raises(ValueError, match="writable_cube_ids is required"):
        backend.delete_node_by_prams(file_ids=["file-1"])

    backend._gremlin.exec.assert_not_called()


def test_delete_node_by_prams_file_ids_require_all_ids_like_the_baseline():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "user_name": ["cube-a"],
            "metadata_json": ['{"file_ids":["file-1","file-2"]}'],
        }
    ]

    # 任一命中不算，全部包含才算（基线 $param IN n.file_ids 的 AND 组）
    assert (
        backend.delete_node_by_prams(writable_cube_ids=["cube-a"], file_ids=["file-1", "file-3"])
        == 0
    )  # file-3 不在节点上
    assert (
        backend.delete_node_by_prams(writable_cube_ids=["cube-a"], file_ids=["file-1", "file-2"])
        == 1
    )  # 全部包含


def test_delete_node_by_prams_rejects_unknown_filter_operator():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = [
        {
            "id": MEMORY_ID,
            "user_name": ["tenant-1"],
            "metadata_json": ['{"key":"alpha"}'],
        }
    ]

    with pytest.raises(ValueError, match="Unsupported filter operator"):
        backend.delete_node_by_prams(filter={"key": {"exec": "drop"}})

    backend._vector_index.delete.assert_not_called()


def test_recovery_vector_failure_is_retryable_before_graph_activation():
    backend = _backend_with_vector_index()
    row = {
        "id": MEMORY_ID,
        "status": ["deleted"],
        "memory_type": ["LongTermMemory"],
        "mem_cube_id": ["cube-1"],
        "embedding_json": ["[0.1, 0.2]"],
        "delete_record_id": ["record-1"],
    }
    # recover issues: (1) projected scan, (2) full-property refetch, (3) the
    # chunked status update. A vector failure must raise before (3) runs.
    backend._gremlin.exec.side_effect = [[row], [row], []]
    backend._vector_index.upsert.side_effect = RuntimeError("vector unavailable")

    with pytest.raises(RuntimeError, match="vector unavailable"):
        backend.recover_memory_by_mem_cube_id("tenant-1", "record-1")

    assert backend._gremlin.exec.call_count == 2  # scan + refetch, no update yet
    backend._vector_index.upsert.side_effect = None
    backend._gremlin.exec.reset_mock()
    backend._gremlin.exec.side_effect = [[row], [row], []]

    assert backend.recover_memory_by_mem_cube_id("tenant-1", "record-1") == 1


# ---------------------------------------------------------------------------
# vec_db attribute contract
#
# organize/handler.py syncs a merged loser's payload status through
# ``getattr(graph_store, "vec_db", None)``. The Neo4j backends expose that
# attribute; if this backend does not, the getattr default turns the sync into
# a silent no-op and archived losers keep showing up in vector recall.
# ---------------------------------------------------------------------------


class _FakeSchemaBuilder:
    """Record a fluent schema chain and return self, like the real builders.

    The real SchemaManager builders (PropertyKey / VertexLabel / EdgeLabel /
    IndexLabel) each return ``self`` from every fluent method, so a chain reads
    ``.asText().ifNotExist().create()``. Recording the whole chain lets a test
    assert the two things that make the bootstrap safe: every chain ends in
    ``create()``, and every chain calls ``ifNotExist()`` before it.
    """

    def __init__(self, recorder, kind, name):
        self._recorder = recorder
        self._kind = kind
        self._name = name

    def __getattr__(self, method):
        def _record(*args):
            self._recorder.append((self._kind, self._name, method, args))
            return self

        return _record


class _FakeSchemaManager:
    def __init__(self):
        self.calls: list[tuple[str, str, str, tuple]] = []
        # Names the list endpoints should report as already existing; tests
        # populate this to exercise the skip path of the bootstrap.
        self.existing: dict[str, set[str]] = {}

    def _existing(self, kind):
        return [SimpleNamespace(name=name) for name in self.existing.get(kind, ())]

    def _builder(self, kind, name):
        return _FakeSchemaBuilder(self.calls, kind, name)

    def propertyKey(self, name):  # noqa: N802 - mirrors pyhugegraph
        return self._builder("propertykey", name)

    def vertexLabel(self, name):  # noqa: N802 - mirrors pyhugegraph
        return self._builder("vertexlabel", name)

    def edgeLabel(self, name):  # noqa: N802 - mirrors pyhugegraph
        return self._builder("edgelabel", name)

    def indexLabel(self, name):  # noqa: N802 - mirrors pyhugegraph
        return self._builder("indexlabel", name)

    def getPropertyKeys(self):  # noqa: N802 - mirrors pyhugegraph
        return self._existing("propertykey")

    def getVertexLabels(self):  # noqa: N802 - mirrors pyhugegraph
        return self._existing("vertexlabel")

    def getEdgeLabels(self):  # noqa: N802 - mirrors pyhugegraph
        return self._existing("edgelabel")

    def getIndexLabels(self):  # noqa: N802 - mirrors pyhugegraph
        return self._existing("indexlabel")


class _FakeHugeClient:
    """Mirror the real 1.7.x constructor exactly.

    Taking ``**kwargs`` here is how the backend drifted to a signature the
    real client rejects: every wrong keyword silently landed in the bucket.
    Keep this signature in sync with ``pyhugegraph.client.PyHugeClient`` so
    the fake fails loudly on the same drift the live client would.
    """

    def __init__(self, url, graph, user, pwd, graphspace=None, timeout=None):
        self.init_kwargs = {
            "url": url,
            "graph": graph,
            "user": user,
            "pwd": pwd,
            "graphspace": graphspace,
            "timeout": timeout,
        }
        self._graph = MagicMock()
        self._gremlin = MagicMock()
        self._traverser = MagicMock()
        self._schema = _FakeSchemaManager()

    def graph(self):
        return self._graph

    def gremlin(self):
        return self._gremlin

    def traverser(self):
        return self._traverser

    def schema(self):
        return self._schema


def _stub_pyhugegraph(monkeypatch, schema_manager=None):
    """Make ``import pyhugegraph`` succeed so the real __init__ can run offline.

    Pass ``schema_manager`` to share one manager across every client the backend
    constructs, so a test can pre-seed "already existing" schema objects (or a
    broken one) before construction instead of reaching in afterwards.
    """
    import sys
    import types

    client_class = _FakeHugeClient
    if schema_manager is not None:

        class _SharedSchemaClient(_FakeHugeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._schema = schema_manager

        client_class = _SharedSchemaClient

    module = types.ModuleType("pyhugegraph")
    client_module = types.ModuleType("pyhugegraph.client")
    client_module.PyHugeClient = client_class
    module.client = client_module
    monkeypatch.setitem(sys.modules, "pyhugegraph", module)
    monkeypatch.setitem(sys.modules, "pyhugegraph.client", client_module)


def test_configured_vector_index_exposes_the_wrapped_vec_db():
    from memos.vec_dbs.index import QdrantVectorIndex

    vec_db = MagicMock()
    index = QdrantVectorIndex(vec_db, "neo4j_vec_db")

    assert index.vec_db is vec_db


def test_init_exposes_vec_db_for_the_handler_payload_sync(monkeypatch):
    from memos.configs.graph_db import HugeGraphDBConfig
    from memos.vec_dbs.index import QdrantVectorIndex

    _stub_pyhugegraph(monkeypatch)
    vec_db = MagicMock()
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    backend = HugeGraphGraphDB(config, vector_index=QdrantVectorIndex(vec_db, "neo4j_vec_db"))

    assert backend.vec_db is vec_db
    # The exact call handler.py makes, so a rename cannot pass unnoticed.
    assert getattr(backend, "vec_db", None) is not None


def test_init_without_a_vector_index_leaves_vec_db_unset(monkeypatch):
    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch)
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    backend = HugeGraphGraphDB(config)

    # None keeps the handler's getattr guard working instead of crashing.
    assert backend.vec_db is None


def test_init_builds_the_client_with_the_url_form_hugegraph_python_17_uses(monkeypatch):
    """hugegraph-python 1.7.0 replaced 1.5.0's ip/port pair with a single url.

    Regression: the backend still passed ip=/port=, so the very first
    construction raised TypeError and this backend had never run against a
    real server. The fake mirrors the real constructor's signature, so this
    also fails loudly if pyhugegraph changes its signature again.
    """
    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch)
    config = HugeGraphDBConfig(
        ip="10.0.0.5",
        port=8080,
        graph="memos_verify",
        graphspace="memos_verify",
        user="admin",
        password="secret",
        user_name="tenant-1",
    )

    backend = HugeGraphGraphDB(config)

    assert backend._client.init_kwargs == {
        "url": "http://10.0.0.5:8080",
        "graph": "memos_verify",
        "user": "admin",
        "pwd": "secret",
        "graphspace": "memos_verify",
        "timeout": None,
    }


def test_init_maps_falsy_graphspace_to_none(monkeypatch):
    """A falsy graphspace must reach the client as None, not "".

    HGraphConfig only skips its /versions network probe when a graphspace is
    actually set, so the exact value handed over decides whether __init__
    makes an HTTP request.
    """
    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch)
    config = HugeGraphDBConfig(ip="10.0.0.5", port=8080, graph="g", user_name="tenant-1")

    backend = HugeGraphGraphDB(config)

    assert backend._client.init_kwargs["graphspace"] is None


# ---------------------------------------------------------------------------
# Schema bootstrap (G2). Until now docker/hugegraph-schema.groovy was the only
# definition of the schema and had to be loaded by hand; a fresh graph failed
# its first write with "Undefined property key: 'user_name'".
# ---------------------------------------------------------------------------


def _schema_calls(backend):
    return backend._client._schema.calls


def _grouped_by_object(calls):
    """Group a recorded chain by (kind, name), keeping call order."""
    grouped: dict[tuple[str, str], list[tuple[str, tuple]]] = {}
    for kind, name, method, args in calls:
        grouped.setdefault((kind, name), []).append((method, args))
    return grouped


def test_init_bootstraps_the_schema_the_queries_depend_on(monkeypatch):
    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch)
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    backend = HugeGraphGraphDB(config)

    calls = _schema_calls(backend)
    grouped = _grouped_by_object(calls)

    # 13 property keys, exactly the set the groovy DDL declares.
    keys = {name for (kind, name) in grouped if kind == "propertykey"}
    assert keys == {
        "id",
        "memory",
        "user_name",
        "memory_type",
        "status",
        "created_at",
        "updated_at",
        "metadata_json",
        "embedding_json",
        "mem_cube_id",
        "relation_type",
        "delete_record_id",
        "delete_time",
    }

    # Vertex label: customize string ids, `id` among the properties but not
    # nullable — docker/hugegraph-schema.groovy lists it only in properties().
    vertex = grouped[("vertexlabel", "Memory")]
    methods = dict(vertex)
    assert "useCustomizeStringId" in methods
    assert "id" in methods["properties"]
    assert "id" not in methods["nullableKeys"]

    # Edge label: MEMORY_RELATION between Memory vertices, relation type carried
    # as a property (D6), user_name nullable for unscoped edges.
    edge = grouped[("edgelabel", "MEMORY_RELATION")]
    methods = dict(edge)
    assert methods["sourceLabel"] == ("Memory",)
    assert methods["targetLabel"] == ("Memory",)
    assert methods["properties"] == ("relation_type", "user_name")
    assert methods["nullableKeys"] == ("user_name",)

    # 7 index labels. There is deliberately no index on user_name alone: the
    # server rejects a secondary index whose fields a composite already covers,
    # and user_name queries use the leading field of the composites.
    index_names = [name for (kind, name) in grouped if kind == "indexlabel"]
    assert len(index_names) == 7
    assert set(index_names) == {
        "memoryByMemoryType",
        "memoryByStatus",
        "memoryByUserAndType",
        "memoryByUserAndStatus",
        "memoryByMemCubeId",
        "relationByUser",
        "memoryByText",
    }
    assert ("indexlabel", "memoryByText", "search", ()) in calls
    assert ("indexlabel", "relationByUser", "onE", ("MEMORY_RELATION",)) in calls

    # Idempotency: every object is created through an ifNotExist() guard, and
    # create() is the last call of each chain.
    for (kind, name), chain in grouped.items():
        methods_in_chain = [method for method, _ in chain]
        assert "ifNotExist" in methods_in_chain, f"{kind}/{name} has no ifNotExist guard"
        assert methods_in_chain[-1] == "create", f"{kind}/{name} never created"


def test_init_keeps_vector_payload_indexes_in_step(monkeypatch):
    """Parity with the Neo4j backend, which indexes the same three payload keys."""
    from memos.configs.graph_db import HugeGraphDBConfig
    from memos.vec_dbs.index import QdrantVectorIndex

    _stub_pyhugegraph(monkeypatch)
    vec_db = MagicMock()
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    HugeGraphGraphDB(config, vector_index=QdrantVectorIndex(vec_db, "neo4j_vec_db"))

    vec_db.ensure_payload_indexes.assert_called_once_with(["user_name", "memory_type", "status"])


def test_init_surfaces_a_schema_bootstrap_failure(monkeypatch):
    """A failed bootstrap stops construction instead of warn-and-continue.

    The Neo4j statements only add indexes, so warning is survivable there.
    Here a missing property key makes every later write fail with "Undefined
    property key", which trades a clear startup error for a confusing one on
    the first write.
    """
    from memos.configs.graph_db import HugeGraphDBConfig

    class _BrokenSchemaManager(_FakeSchemaManager):
        def indexLabel(self, name):  # noqa: N802 - mirrors pyhugegraph
            raise RuntimeError("schema API unreachable")

    _stub_pyhugegraph(monkeypatch, schema_manager=_BrokenSchemaManager())
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    with pytest.raises(RuntimeError, match="schema API unreachable"):
        HugeGraphGraphDB(config)


def test_init_skips_schema_objects_that_already_exist(monkeypatch):
    """Second and later constructions must not re-POST anything.

    The first construction against memos_verify created the schema; the second
    one re-POSTed an index the list already showed and died on the server's own
    duplicate check. Deciding from the list endpoints keeps re-runs no-ops.
    """
    from memos.configs.graph_db import HugeGraphDBConfig

    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    # Fresh graph: everything gets created.
    fresh_schema = _FakeSchemaManager()
    _stub_pyhugegraph(monkeypatch, schema_manager=fresh_schema)
    HugeGraphGraphDB(config)
    created = {(kind, name) for kind, name, method, _ in fresh_schema.calls if method == "create"}
    assert ("propertykey", "user_name") in created
    assert ("indexlabel", "memoryByText") in created

    # Populated graph: nothing gets created.
    warm_schema = _FakeSchemaManager()
    warm_schema.existing = {
        "propertykey": {
            "id",
            "memory",
            "user_name",
            "memory_type",
            "status",
            "created_at",
            "updated_at",
            "metadata_json",
            "embedding_json",
            "mem_cube_id",
            "relation_type",
            "delete_record_id",
            "delete_time",
        },
        "vertexlabel": {"Memory"},
        "edgelabel": {"MEMORY_RELATION"},
        "indexlabel": {
            "memoryByMemoryType",
            "memoryByStatus",
            "memoryByUserAndType",
            "memoryByUserAndStatus",
            "memoryByMemCubeId",
            "relationByUser",
            "memoryByText",
        },
    }
    _stub_pyhugegraph(monkeypatch, schema_manager=warm_schema)
    HugeGraphGraphDB(config)
    assert warm_schema.calls == [], f"bootstrap re-created objects: {warm_schema.calls}"


# ---------------------------------------------------------------------------
# Gremlin envelope unwrapping. pyhugegraph's exec() returns the server's
# "result" object ({"data": [...], "meta": ...}), not the row list; the backend
# used to wrap that envelope again, so a miss looked like one found row and
# add_node appended to a vertex that did not exist (PUT 404 on memos_verify).
# The mocks in this file return row lists, which the normalization also
# accepts; these tests drive the envelope shape the real client hands over.
# ---------------------------------------------------------------------------


def test_missing_vertex_reads_as_missing_through_the_real_envelope_shape():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = {"data": [], "meta": {}}

    assert backend._get_vertex_by_node_id("missing-id") is None


def test_add_node_creates_instead_of_appending_when_envelope_says_missing():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = {"data": [], "meta": {}}

    backend.add_node(MEMORY_ID, "memory text", {"memory_type": "LongTermMemory"})

    backend._g.addVertex.assert_called_once()
    backend._g.appendVertex.assert_not_called()


def test_found_vertex_is_unwrapped_from_the_envelope():
    """The envelope test drives the gremlin fallback, which only runs when
    the REST call itself errors (an empty REST answer is authoritative)."""
    backend = _backend_with_vector_index()
    backend._g.getVerticesById = MagicMock(side_effect=RuntimeError("REST down"))
    backend._gremlin.exec.return_value = {
        "data": [{"id": [MEMORY_ID], "user_name": ["tenant-1"]}],
        "meta": {},
    }

    assert backend._get_vertex_by_node_id(MEMORY_ID) == {
        "id": MEMORY_ID,
        "user_name": "tenant-1",
    }


def test_gremlin_exec_rejects_an_unexpected_result_shape():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.return_value = {"oops": True}

    with pytest.raises(TypeError, match="Unexpected gremlin result shape"):
        backend._gremlin_exec("g.V().count()")


# ---------------------------------------------------------------------------
# Cross-replica schema-cache lag. Measured against memos_verify: half an hour
# after the indexes were created, 3 of 12 identical queries still failed with
# a "Can't query by label index" error naming CREATING as the status, while
# the rest succeeded — per-replica caches disagreeing, not a short build
# window. The server rejects at planning time, so retrying is safe.
# ---------------------------------------------------------------------------

_INDEX_NOT_READY_MESSAGE = (
    "Server Exception: Can't query by label index 'memoryByUserAndType(id=4)' "
    "due to it's in status CREATING(CREATED expected)"
)


def test_gremlin_exec_retries_while_a_replica_reports_index_not_created(monkeypatch):
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = [
        RuntimeError(_INDEX_NOT_READY_MESSAGE),
        RuntimeError(_INDEX_NOT_READY_MESSAGE),
        {"data": [{"id": [MEMORY_ID]}], "meta": {}},
    ]
    sleeps: list[float] = []
    monkeypatch.setattr(
        "memos.graph_dbs.hugegraph.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    assert backend._gremlin_exec("g.V().limit(1)") == [{"id": [MEMORY_ID]}]
    assert backend._gremlin.exec.call_count == 3
    assert sleeps == [1.5, 1.5]


def test_gremlin_exec_does_not_retry_unrelated_errors():
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = RuntimeError("Undefined property key: 'x'")

    with pytest.raises(RuntimeError, match="Undefined property key"):
        backend._gremlin_exec("g.V().limit(1)")
    assert backend._gremlin.exec.call_count == 1


def test_gremlin_exec_gives_up_after_bounded_retries(monkeypatch):
    backend = _backend_with_vector_index()
    backend._gremlin.exec.side_effect = RuntimeError(_INDEX_NOT_READY_MESSAGE)
    monkeypatch.setattr("memos.graph_dbs.hugegraph.time.sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="CREATED expected"):
        backend._gremlin_exec("g.V().limit(1)")
    assert backend._gremlin.exec.call_count == 5


class _RepeatedIndexBuilder(_FakeSchemaBuilder):
    """Reject create() for memoryByText the way a lagging replica would."""

    def create(self):
        self._recorder.append((self._kind, self._name, "create", ()))
        if self._name == "memoryByText":
            raise RuntimeError(
                "Server Exception: Repeated new index label memoryByText(SEARCH) "
                "with fields [memory] due to existed index label memoryByText(SEARCH)"
            )
        return self


class _LaggingReplicaSchemaManager(_FakeSchemaManager):
    def indexLabel(self, name):  # noqa: N802 - mirrors pyhugegraph
        return _RepeatedIndexBuilder(self.calls, "indexlabel", name)


def test_schema_bootstrap_tolerates_a_replica_that_already_created_the_index(monkeypatch):
    """A stale list plus a server-side duplicate check must not fail construction."""
    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch, schema_manager=_LaggingReplicaSchemaManager())
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    backend = HugeGraphGraphDB(config)  # must not raise

    created_indexes = {
        name
        for kind, name, method, _ in _schema_calls(backend)
        if kind == "indexlabel" and method == "create"
    }
    assert "memoryByText" in created_indexes


# ---------------------------------------------------------------------------
# No-vector-index contract (G3). The Neo4j configs require vec_config outright,
# so a vector-less HugeGraph backend is a deliberate, unusual choice: say so at
# startup, and make the vector paths fail loudly instead of quietly dropping
# embeddings or returning empty recall.
# ---------------------------------------------------------------------------


def _backend_without_vector_index() -> HugeGraphGraphDB:
    backend = HugeGraphGraphDB.__new__(HugeGraphGraphDB)
    backend.user_name = "tenant-1"
    backend.config = SimpleNamespace(graph="hugegraph")
    backend._g = MagicMock()
    backend._traverser = MagicMock()
    backend._vector_index = None
    backend.vec_db = None
    backend._gremlin = MagicMock()
    backend._gremlin.exec.return_value = {"data": [], "meta": {}}
    return backend


def test_construction_without_vector_index_warns(monkeypatch, caplog):
    import logging

    from memos.configs.graph_db import HugeGraphDBConfig

    _stub_pyhugegraph(monkeypatch)
    config = HugeGraphDBConfig(ip="127.0.0.1", port=8080, graph="g", user_name="tenant-1")

    with caplog.at_level(logging.WARNING):
        HugeGraphGraphDB(config)

    assert any("no vector index configured" in record.message for record in caplog.records)


def test_search_by_embedding_raises_without_a_vector_index():
    """Silent emptiness used to be how semantic recall failed here."""
    backend = _backend_without_vector_index()

    with pytest.raises(RuntimeError, match="no vector index configured"):
        backend.search_by_embedding([0.1, 0.2, 0.3])


def test_add_node_with_embedding_raises_without_a_vector_index():
    """Dropping the embedding would write rows vector recall can never see."""
    backend = _backend_without_vector_index()

    with pytest.raises(RuntimeError, match="no vector index configured"):
        backend.add_node(
            MEMORY_ID, "memory text", {"memory_type": "LongTermMemory", "embedding": [0.1, 0.2]}
        )


def test_delete_paths_stay_usable_without_a_vector_index():
    """A store that never accepted embedding writes has no vectors to delete.

    Raising here would break delete_node and clear for graph-only data, so the
    delete path is deliberately the one vector path that stays a no-op.
    """
    backend = _backend_without_vector_index()
    backend._get_vertex_by_node_id = MagicMock(
        return_value={"id": MEMORY_ID, "user_name": "tenant-1"}
    )

    backend.delete_node(MEMORY_ID)
    backend.clear()

    backend._g.removeVertexById.assert_called_once_with(MEMORY_ID)


# ---------------------------------------------------------------------------
# search_by_embedding parameter parity (G4): search_filter narrows inside the
# vector store; filter and knowledgebase_ids post-filter the candidates against
# the graph; threshold keeps scoreless hits; return_fields are validated.
# ---------------------------------------------------------------------------


def test_embedding_search_applies_search_filter_to_the_vector_payload():
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = []

    backend.search_by_embedding([0.1], search_filter={"mem_cube_id": "kb-1"})

    payload_filter = backend._vector_index.search.call_args.args[2]
    assert payload_filter["mem_cube_id"] == "kb-1"
    assert payload_filter["vector_sync"] == "success"


def test_embedding_search_honours_cube_name_override_for_the_vector_filter():
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = []

    backend.search_by_embedding([0.1], cube_name="cube-owner")

    payload_filter = backend._vector_index.search.call_args.args[2]
    assert payload_filter["user_name"] == "cube-owner"


def test_embedding_search_post_filters_candidates_by_knowledgebase_ids():
    """kb ids act as additional user names, ORed with the tenant (baseline)."""
    other_id = "22222222-2222-4222-8222-222222222222"
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [
        SimpleNamespace(id=other_id, score=0.9),
        SimpleNamespace(id=MEMORY_ID, score=0.8),
    ]
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(properties={"id": other_id, "memory": "kb node", "user_name": "kb-7"}),
        SimpleNamespace(properties={"id": MEMORY_ID, "memory": "mine", "user_name": "tenant-1"}),
    ]

    results = backend.search_by_embedding([0.1], knowledgebase_ids=["kb-7"])

    assert [r["id"] for r in results] == [other_id, MEMORY_ID]


def test_embedding_search_post_filters_candidates_by_graph_filter():
    other_id = "22222222-2222-4222-8222-222222222222"
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [
        SimpleNamespace(id=MEMORY_ID, score=0.9),
        SimpleNamespace(id=other_id, score=0.8),
    ]
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(
            properties={
                "id": MEMORY_ID,
                "memory": "important",
                "user_name": "tenant-1",
                "memory_type": "LongTermMemory",
                "metadata_json": '{"importance": 5}',
            }
        ),
        SimpleNamespace(
            properties={
                "id": other_id,
                "memory": "trivial",
                "user_name": "tenant-1",
                "memory_type": "WorkingMemory",
                "metadata_json": '{"importance": 1}',
            }
        ),
    ]

    results = backend.search_by_embedding(
        [0.1],
        filter={"and": [{"memory_type": "LongTermMemory"}, {"importance": {"gte": 3}}]},
    )
    assert [r["id"] for r in results] == [MEMORY_ID]

    results = backend.search_by_embedding(
        [0.1],
        filter={"or": [{"memory_type": "WorkingMemory"}, {"importance": {"gte": 3}}]},
    )
    assert {r["id"] for r in results} == {MEMORY_ID, other_id}


def test_embedding_search_rejects_unknown_graph_filter_operator():
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [SimpleNamespace(id=MEMORY_ID, score=0.9)]
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(properties={"id": MEMORY_ID, "memory": "m", "user_name": "tenant-1"})
    ]

    with pytest.raises(ValueError, match="Unsupported filter operator"):
        backend.search_by_embedding([0.1], filter={"memory": {"fuzzy": "x"}})


def test_embedding_search_threshold_keeps_scoreless_hits_like_the_baseline():
    other_id = "22222222-2222-4222-8222-222222222222"
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [
        SimpleNamespace(id=MEMORY_ID, score=None),
        SimpleNamespace(id=other_id, score=0.4),
    ]

    results = backend.search_by_embedding([0.1], threshold=0.5)

    assert [r["id"] for r in results] == [MEMORY_ID]
    assert results[0]["score"] is None


def test_embedding_search_validates_return_fields():
    backend = _backend_with_vector_index()
    backend._vector_index.search.return_value = [SimpleNamespace(id=MEMORY_ID, score=0.9)]
    backend._g.getVerticesById.return_value = [
        SimpleNamespace(
            properties={
                "id": MEMORY_ID,
                "memory": "the text",
                "user_name": "tenant-1",
                "memory_type": "LongTermMemory",
                "metadata_json": "{}",
            }
        )
    ]

    results = backend.search_by_embedding(
        [0.1], return_fields=["memory", "memory_type", "drop; table"]
    )

    assert results[0]["memory"] == "the text"
    assert results[0]["memory_type"] == "LongTermMemory"
    assert "drop; table" not in results[0]
