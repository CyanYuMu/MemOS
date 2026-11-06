"""A tool whose definition changed keeps one live schema; the old one becomes history."""

import json
import uuid

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize import tool_schema_versions as tsv
from tests.memories.textual.fake_graph import FakeGraph


USER = "agent-1"
TENANT = "cube-1"


class SchemaGraph(FakeGraph):
    def get_by_metadata(self, filters, user_name=None, status=None, **_):
        wanted = {f["field"]: f["value"] for f in filters}
        return [
            node_id
            for node_id, node in self.nodes.items()
            if all(node["metadata"].get(k) == v for k, v in wanted.items())
            and (status is None or node["metadata"].get("status", "activated") == status)
        ]


def _schema(name, description="", *, user=USER, created="2026-10-01T10:00:00+00:00"):
    body = {"type": "function", "function": {"name": name, "description": description}}
    return TextualMemoryItem(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user}|{name}|{description}")),
        memory=json.dumps(body),
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="ToolSchemaMemory",
            status="activated",
            user_id=user,
            created_at=created,
            embedding=[0.1],
        ),
    )


def _write(graph, *items):
    for item in tsv.skip_live_schemas(graph, list(items), TENANT):
        graph.seed(item)


def test_a_changed_definition_archives_the_old_schema_into_the_new_ones_history():
    graph = SchemaGraph()
    old = _schema("search", "v1", created="2026-10-01T10:00:00+00:00")
    graph.seed(old)
    new = _schema("search", "v2", created="2026-10-05T10:00:00+00:00")
    _write(graph, new)

    assert tsv.fold_replaced_schemas(graph, [new], TENANT) == 1

    assert graph.status(old.id) == "archived"
    assert graph.status(new.id) == "activated"
    assert (old.id, new.id) in graph.merged_to()
    assert [h["memory"] for h in graph.history(new.id)] == [old.memory]
    assert graph.history(new.id)[0]["update_type"] == "update"
    assert graph.nodes[new.id]["metadata"]["version"] == 2
    assert (
        graph.nodes[old.id]["metadata"]["valid_to"] == graph.nodes[new.id]["metadata"]["valid_from"]
    )
    assert graph.deleted == []


def test_a_chain_of_redefinitions_keeps_every_earlier_definition_once():
    graph = SchemaGraph()
    for index, description in enumerate(["v1", "v2", "v3"]):
        item = _schema("search", description, created=f"2026-10-0{index + 1}T10:00:00+00:00")
        _write(graph, item)
        tsv.fold_replaced_schemas(graph, [item], TENANT)

    head = _schema("search", "v3")
    assert graph.status(head.id) == "activated"
    assert [json.loads(h["memory"])["function"]["description"] for h in graph.history(head.id)] == [
        "v1",
        "v2",
    ]
    assert [h["version"] for h in graph.history(head.id)] == [1, 2]
    assert sum(1 for n in graph.nodes if graph.status(n) == "activated") == 1


def test_repeating_the_same_schema_writes_nothing_and_keeps_the_history():
    graph = SchemaGraph()
    graph.seed(_schema("search", "v1", created="2026-10-01T10:00:00+00:00"))
    new = _schema("search", "v2", created="2026-10-05T10:00:00+00:00")
    _write(graph, new)
    tsv.fold_replaced_schemas(graph, [new], TENANT)
    history = graph.history(new.id)

    again = _schema("search", "v2", created="2026-10-09T10:00:00+00:00")
    kept = tsv.skip_live_schemas(graph, [again], TENANT)

    assert kept == []
    assert graph.history(new.id) == history
    assert tsv.fold_replaced_schemas(graph, kept, TENANT) == 0


def test_other_tools_and_other_users_are_left_alone():
    graph = SchemaGraph()
    other_tool = _schema("fetch", "v1")
    other_user = _schema("search", "v1", user="agent-2")
    graph.seed(other_tool)
    graph.seed(other_user)
    new = _schema("search", "v2")
    _write(graph, new)

    assert tsv.fold_replaced_schemas(graph, [new], TENANT) == 0

    assert graph.status(other_tool.id) == graph.status(other_user.id) == "activated"
    assert graph.history(new.id) == []


def test_a_schema_that_never_got_stored_leaves_the_old_one_live():
    graph = SchemaGraph()
    old = _schema("search", "v1")
    graph.seed(old)

    assert tsv.fold_replaced_schemas(graph, [_schema("search", "v2")], TENANT) == 0

    assert graph.status(old.id) == "activated"


def test_a_failing_lookup_never_breaks_the_write():
    graph = SchemaGraph()
    graph.seed(_schema("search", "v1"))
    new = _schema("search", "v2")
    _write(graph, new)

    def broken(*_a, **_k):
        raise RuntimeError("graph down")

    graph.get_by_metadata = broken

    assert tsv.fold_replaced_schemas(graph, [new], TENANT) == 0
    assert graph.status(new.id) == "activated"
