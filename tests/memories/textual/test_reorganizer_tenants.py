"""Regression tests for tenant-aware structure optimization.

The structure optimizer is a background thread: it has no request context, so
it used to fall back to ``graph_store.config.user_name`` while every write
carried the cube id. When these values differ, so the optimizer ran forever and never produced a single
PARENT edge. These tests pin the fix.
"""

from unittest.mock import MagicMock, call, patch

from memos.memories.textual.tree_text_memory.organize.reorganizer import (
    GraphStructureReorganizer,
)


_MODULE = "memos.memories.textual.tree_text_memory.organize.reorganizer"


def _make_reorganizer(graph_store):
    """Build a reorganizer without spawning its background threads."""
    with (
        patch(f"{_MODULE}.ContextThread"),
        patch(f"{_MODULE}.RelationAndReasoningDetector"),
        patch(f"{_MODULE}.NodeHandler"),
    ):
        return GraphStructureReorganizer(
            graph_store=graph_store,
            llm=MagicMock(),
            embedder=MagicMock(),
            is_reorganize=True,
        )


def test_resolve_optimize_targets_uses_identities_found_in_store():
    graph_store = MagicMock()
    graph_store.list_user_names.return_value = ["example-agent", "hermes-agent"]

    targets = _make_reorganizer(graph_store)._resolve_optimize_targets()

    assert targets == ["example-agent", "hermes-agent"]


def test_resolve_optimize_targets_falls_back_when_store_lists_nothing():
    graph_store = MagicMock()
    graph_store.list_user_names.return_value = []

    assert _make_reorganizer(graph_store)._resolve_optimize_targets() == [None]


def test_resolve_optimize_targets_survives_store_errors():
    graph_store = MagicMock()
    graph_store.list_user_names.side_effect = RuntimeError("neo4j unavailable")

    assert _make_reorganizer(graph_store)._resolve_optimize_targets() == [None]


def test_optimize_all_tenants_passes_discovered_user_name():
    """The regression: optimize_structure must receive the real tenant identity."""
    graph_store = MagicMock()
    graph_store.list_user_names.return_value = ["example-agent"]
    reorganizer = _make_reorganizer(graph_store)
    reorganizer.optimize_structure = MagicMock()

    reorganizer._optimize_all_tenants()

    assert reorganizer.optimize_structure.call_args_list == [
        call(scope="LongTermMemory", user_name="example-agent"),
        call(scope="UserMemory", user_name="example-agent"),
    ]


def test_optimize_structure_forwards_user_name_to_the_store():
    graph_store = MagicMock()
    graph_store.node_not_exist.return_value = True
    reorganizer = _make_reorganizer(graph_store)

    reorganizer.optimize_structure(scope="LongTermMemory", user_name="example-agent")

    graph_store.node_not_exist.assert_called_once_with("LongTermMemory", user_name="example-agent")


def test_optimize_structure_tracks_progress_per_tenant():
    """One tenant optimizing must not mask another tenant's run."""
    graph_store = MagicMock()
    graph_store.node_not_exist.return_value = False
    graph_store.get_structure_optimization_candidates.return_value = []
    reorganizer = _make_reorganizer(graph_store)

    reorganizer.optimize_structure(scope="LongTermMemory", user_name="example-agent")
    reorganizer.optimize_structure(scope="LongTermMemory", user_name="hermes-agent")

    assert graph_store.node_not_exist.call_count == 2
    assert set(reorganizer._is_optimizing) == {
        ("LongTermMemory", "example-agent"),
        ("LongTermMemory", "hermes-agent"),
    }


def test_optimize_structure_skips_when_same_tenant_is_already_running():
    graph_store = MagicMock()
    graph_store.node_not_exist.return_value = False
    graph_store.get_structure_optimization_candidates.return_value = []
    reorganizer = _make_reorganizer(graph_store)
    reorganizer._is_optimizing[("LongTermMemory", "example-agent")] = True

    reorganizer.optimize_structure(scope="LongTermMemory", user_name="example-agent")

    graph_store.node_not_exist.assert_not_called()


def _node(node_id):
    node = MagicMock()
    node.id = node_id
    return node


def _run_cluster(reorganizer, cluster_nodes, sub_clusters):
    """Drive _process_cluster_and_write with a stubbed LLM sub-clustering step."""
    reorganizer._local_subcluster = MagicMock(return_value=sub_clusters)
    reorganizer._summarize_cluster = MagicMock(side_effect=lambda nodes, scope: _node("p"))
    reorganizer._create_parent_node = MagicMock()
    reorganizer._link_cluster_nodes = MagicMock()

    reorganizer._process_cluster_and_write(
        cluster_nodes,
        "LongTermMemory",
        local_tree_threshold=10,
        min_cluster_size=4,
        min_subcluster_size=2,
        user_name="example-agent",
    )


def test_two_and_three_item_subclusters_are_kept():
    """_local_subcluster's prompt promises 2-10 item sub-clusters.

    Filtering them at min_cluster_size (4) discarded two thirds of what the LLM
    returned, and those nodes came back as candidates on every later pass, so
    they never reached the graph.
    """
    graph_store = MagicMock()
    reorganizer = _make_reorganizer(graph_store)
    cluster_nodes = [_node(f"n{i}") for i in range(8)]

    _run_cluster(
        reorganizer,
        cluster_nodes,
        [cluster_nodes[0:2], cluster_nodes[2:5], cluster_nodes[5:8]],
    )

    assert reorganizer._link_cluster_nodes.call_count == 3
    linked = [len(call.args[1]) for call in reorganizer._link_cluster_nodes.call_args_list]
    assert linked == [2, 3, 3]


def test_singleton_subclusters_are_still_dropped():
    graph_store = MagicMock()
    reorganizer = _make_reorganizer(graph_store)
    cluster_nodes = [_node(f"n{i}") for i in range(6)]

    _run_cluster(reorganizer, cluster_nodes, [cluster_nodes[0:1], cluster_nodes[1:3]])

    assert reorganizer._link_cluster_nodes.call_count == 1
    assert len(reorganizer._link_cluster_nodes.call_args.args[1]) == 2


def test_subcluster_filter_does_not_change_the_cluster_size_guard():
    """min_cluster_size still guards which clusters are processed at all."""
    graph_store = MagicMock()
    reorganizer = _make_reorganizer(graph_store)
    reorganizer._local_subcluster = MagicMock()

    reorganizer._process_cluster_and_write(
        [_node(f"n{i}") for i in range(4)],
        "LongTermMemory",
        local_tree_threshold=10,
        min_cluster_size=4,
        min_subcluster_size=2,
        user_name="example-agent",
    )

    reorganizer._local_subcluster.assert_not_called()
