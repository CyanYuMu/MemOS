import uuid

from unittest.mock import MagicMock

import pytest

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.manager import MemoryManager


@pytest.fixture
def mock_graph_store():
    store = MagicMock()
    store.get_node.return_value = {
        "id": str(uuid.uuid4()),
        "memory": "old text",
        "metadata": {
            "confidence": 90,
            "background": "",
            "tags": [],
            "sources": [],
            "usage": [],
        },
    }
    store.search_by_embedding.return_value = [{"id": str(uuid.uuid4()), "score": 0.95}]
    store.get_edges.return_value = [{"from": "from_id", "to": "to_id", "type": "RELATE"}]
    store.edge_exists.return_value = False
    return store


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.embed.side_effect = lambda texts: [[0.1] * 5 for _ in texts]
    return embedder


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.run.side_effect = lambda *args, **kwargs: "mock_output"
    return llm


@pytest.fixture
def memory_manager(mock_graph_store, mock_embedder, mock_llm):
    return MemoryManager(
        graph_store=mock_graph_store,
        embedder=mock_embedder,
        llm=mock_llm,
    )


def test_add_and_replace_working_memory(memory_manager):
    memory = TextualMemoryItem(
        memory="test",
        metadata=TreeNodeTextualMemoryMetadata(embedding=[0.1] * 5, memory_type="WorkingMemory"),
    )
    memory_manager.add([memory])
    memory_manager.replace_working_memory([memory])
    assert memory_manager.graph_store.add_node.called


def test_process_memory_adds_nodes(memory_manager):
    memory = TextualMemoryItem(
        memory="test",
        metadata=TreeNodeTextualMemoryMetadata(
            embedding=[0.1] * 5,
            memory_type="UserMemory",
            tags=["test"],
            key="topic",
            confidence=80.0,
        ),
    )
    memory_manager._process_memory(memory)  # Only pass the single memory item
    assert memory_manager.graph_store.add_node.called


def test_add_to_graph_memory_merges(memory_manager, mock_graph_store):
    memory = TextualMemoryItem(
        memory="to merge",
        metadata=TreeNodeTextualMemoryMetadata(
            embedding=[0.1] * 5, memory_type="UserMemory", confidence=80.0
        ),
    )
    memory_manager._add_to_graph_memory(memory, "UserMemory")
    assert mock_graph_store.add_node.called


def test_add_to_graph_memory_creates_new_node(memory_manager, mock_graph_store):
    mock_graph_store.search_by_embedding.return_value = [{"id": "id1", "score": 0.5}]
    memory = TextualMemoryItem(
        memory="new memory",
        metadata=TreeNodeTextualMemoryMetadata(
            embedding=[0.1] * 5,
            memory_type="LongTermMemory",
            tags=["test"],
            key="topic",
        ),
    )
    memory_manager._add_to_graph_memory(memory, "LongTermMemory")
    assert mock_graph_store.add_node.called


def test_add_returns_written_node_ids(memory_manager):
    memory = TextualMemoryItem(
        memory="test memory",
        metadata=TreeNodeTextualMemoryMetadata(embedding=[0.1] * 5, memory_type="UserMemory"),
    )
    ids = memory_manager.add([memory])
    assert isinstance(ids, list)
    assert all(isinstance(i, str) for i in ids)
    assert len(ids) > 0


def _graph_item(memory_id: str) -> TextualMemoryItem:
    return TextualMemoryItem(
        id=memory_id,
        memory=f"memory {memory_id}",
        metadata=TreeNodeTextualMemoryMetadata(embedding=[0.1] * 5, memory_type="UserMemory"),
    )


def test_batch_add_does_not_report_ids_when_the_store_write_fails(memory_manager):
    memory = _graph_item("11111111-1111-1111-1111-111111111111")
    memory_manager.graph_store.add_nodes_batch.side_effect = RuntimeError("db down")

    ids = memory_manager.add([memory], use_batch=True, mode="async")

    # A failed batch must not be reported as written; callers persist these ids.
    assert ids == []


def test_batch_add_only_reports_ids_confirmed_by_the_store(memory_manager):
    first = _graph_item("11111111-1111-1111-1111-111111111111")
    second = _graph_item("22222222-2222-2222-2222-222222222222")
    # Simulate a store that silently skipped the second node during preparation.
    memory_manager.graph_store.add_nodes_batch.return_value = [first.id]

    ids = memory_manager.add([first, second], use_batch=True, mode="async")

    assert ids == [first.id]


class _FakeNode:
    def __init__(self, node_id):
        self.id = node_id


class _InPlaceNodeMutatingReorganizer:
    """Mimics ``GraphStructureReorganizer._convert_id_to_node``: replaces ids with nodes."""

    def __init__(self, graph_store):
        self.graph_store = graph_store
        self.seen = []

    def add_message(self, message):
        # The consumer thread runs concurrently with the API thread that is still
        # holding the list handed to us.
        for i, node in enumerate(message.after_node):
            message.after_node[i] = _FakeNode(node)
        self.seen.append(message)

    def wait_until_current_task_done(self):
        return None

    def stop(self):
        return None


def test_add_returns_string_ids_even_when_the_reorganizer_converts_in_place(memory_manager):
    """Regression: write path 500'd with "GraphDBNode is not JSON serializable".

    The reorganizer queue converts ids to nodes; if it does that on the list
    ``add()`` returns, the caller's ``json.dumps(mem_ids)`` breaks. ``add()`` must
    hand the queue a copy and keep its own result plain strings.
    """
    memory = _graph_item("11111111-1111-1111-1111-111111111111")
    memory_manager.graph_store.add_nodes_batch.return_value = [memory.id]
    reorganizer = _InPlaceNodeMutatingReorganizer(memory_manager.graph_store)
    memory_manager.reorganizer = reorganizer
    memory_manager.is_reorganize = True

    ids = memory_manager.add([memory], use_batch=True, mode="async")

    assert reorganizer.seen, "reorganizer 没有收到 add 消息"
    # The queue really did convert its copy to nodes...
    assert not isinstance(reorganizer.seen[0].after_node[0], str)
    # ...but the caller's result is still plain ids.
    assert ids == [memory.id]
    assert all(isinstance(i, str) for i in ids)


def test_convert_id_to_node_does_not_mutate_the_callers_list():
    from memos.memories.textual.tree_text_memory.organize.reorganizer import (
        GraphStructureReorganizer,
        QueueMessage,
    )

    reorganizer = object.__new__(GraphStructureReorganizer)
    store = MagicMock()
    store.get_node.return_value = {
        "id": "aaaaaaaa-1111-1111-1111-111111111111",
        "memory": "text",
        "metadata": {"memory_type": "UserMemory"},
    }
    reorganizer.graph_store = store

    caller_list = ["aaaaaaaa-1111-1111-1111-111111111111"]
    message = QueueMessage(op="add", after_node=caller_list, user_name="cube")
    returned = reorganizer._convert_id_to_node(message)

    assert returned.after_node and not isinstance(returned.after_node[0], str)
    assert caller_list == ["aaaaaaaa-1111-1111-1111-111111111111"]


def test_as_memory_ids_normalizes_nodes_dicts_and_strings():
    # 先导入 API handler：single_cube 与 add_handler 互相引用，单独导入会 circular import。
    import memos.api.handlers.add_handler  # noqa: F401

    from memos.multi_mem_cube.single_cube import _as_memory_ids

    class _Node:
        def __init__(self, node_id):
            self.id = node_id

    assert _as_memory_ids(["a", _Node("b"), {"id": "c"}, {"memory": "no id"}, None]) == [
        "a",
        "b",
        "c",
        "",
        "None",
    ]
