from types import SimpleNamespace

from memos.api.handlers import memory_handler
from memos.api.product_models import GetMemoryRequest
from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree import TreeTextMemory


def _node(node_id, cube, status="activated", memory_type="LongTermMemory"):
    return {
        "id": node_id,
        "memory": f"memory {node_id}",
        "metadata": {"user_name": cube, "status": status, "memory_type": memory_type},
    }


class FakeTextMem:
    """Filters the way the graph store does: by cube when one is named, by status when listed."""

    def __init__(self, nodes):
        self.nodes = nodes

    def get(self, memory_id, user_name=None):
        for node in self.nodes:
            if node["id"] == memory_id and user_name in (None, node["metadata"]["user_name"]):
                return TextualMemoryItem(
                    id=node["id"],
                    memory=node["memory"],
                    metadata=TreeNodeTextualMemoryMetadata(**node["metadata"]),
                )
        raise ValueError(f"Memory with ID {memory_id} not found")

    def get_by_ids(self, memory_ids, user_name=None):
        return [
            n
            for n in self.nodes
            if n["id"] in memory_ids and user_name in (None, n["metadata"]["user_name"])
        ]

    def get_all(self, *, user_name=None, memory_type=None, status=None, **_):
        picked = [
            n
            for n in self.nodes
            if n["metadata"]["user_name"] == user_name
            and n["metadata"]["memory_type"] in (memory_type or [])
            and n["metadata"]["status"] in (status or ["activated", "archived", "frozen"])
        ]
        return {"nodes": picked, "total_nodes": len(picked)}


A_LIVE = "1a000000-0000-4000-8000-00000000000a"
A_FROZEN = "1a000000-0000-4000-8000-0000000000af"
NODES = [
    _node("1a000000-0000-4000-8000-00000000000a", "cube-a"),
    _node("1a000000-0000-4000-8000-0000000000a0", "cube-a", status="archived"),
    _node("1a000000-0000-4000-8000-0000000000af", "cube-a", status="frozen"),
    _node("1b000000-0000-4000-8000-00000000000b", "cube-b"),
]
CUBE = SimpleNamespace(text_mem=FakeTextMem(NODES))


def test_a_memory_read_by_id_can_be_pinned_to_one_cube():
    other = memory_handler.handle_get_memory(
        "1b000000-0000-4000-8000-00000000000b", CUBE, mem_cube_id="cube-a"
    )
    own = memory_handler.handle_get_memory(
        "1b000000-0000-4000-8000-00000000000b", CUBE, mem_cube_id="cube-b"
    )
    owner_lookup = memory_handler.handle_get_memory("1b000000-0000-4000-8000-00000000000b", CUBE)

    assert other.data is None and "not found" in other.message
    assert own.data["id"] == owner_lookup.data["id"] == "1b000000-0000-4000-8000-00000000000b"


def test_reading_by_ids_honours_the_named_cube():
    ids = ["1a000000-0000-4000-8000-00000000000a", "1b000000-0000-4000-8000-00000000000b"]

    got = memory_handler.handle_get_memory_by_ids(ids, CUBE, mem_cube_id="cube-b").data["memories"]

    assert [n["id"] for n in got] == ["1b000000-0000-4000-8000-00000000000b"]


def test_the_memory_list_leaves_out_archived_nodes_but_keeps_frozen_ones():
    request = GetMemoryRequest(mem_cube_id="cube-a", user_id="u1")

    listed = memory_handler.handle_get_memories(request, CUBE).data["text_mem"][0]

    assert {n["id"] for n in listed["memories"]} == {A_LIVE, A_FROZEN}


def test_tree_memory_passes_a_status_filter_to_the_graph_only_when_one_is_given():
    seen = []

    class Graph:
        def export_graph(self, **kwargs):
            seen.append(kwargs)
            return {"nodes": [], "total_nodes": 0}

    tree = object.__new__(TreeTextMemory)
    tree.graph_store = Graph()

    tree.get_all(user_name="c")
    tree.get_all(user_name="c", status=["activated"])

    assert "status" not in seen[0] and seen[1]["status"] == ["activated"]
