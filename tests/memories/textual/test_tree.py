import uuid

from unittest.mock import MagicMock, patch

import pytest

from memos.configs.memory import TreeTextMemoryConfig
from memos.exceptions import MemoryDeleteError
from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree import TreeTextMemory


@pytest.fixture
def mock_config():
    config = TreeTextMemoryConfig(
        extractor_llm={
            "backend": "openai",
            "config": {
                "model_name_or_path": "gpt-4o",
                "api_key": "test_api_key",
            },
        },
        dispatcher_llm={
            "backend": "openai",
            "config": {
                "model_name_or_path": "gpt-4o",
                "api_key": "test_api_key",
            },
        },
        embedder={
            "backend": "ollama",
            "config": {
                "model_name_or_path": "default",
            },
        },
        graph_db={
            "backend": "neo4j",
            "config": {
                "uri": "bolt://localhost:7687",
                "user": "neo4j",
                "password": "test_password",
                "db_name": "test",
            },
        },
        memory_filename="memory.json",
    )
    return config


@pytest.fixture
def mock_tree_text_memory(mock_config):
    with (
        patch("memos.llms.factory.LLMFactory.from_config"),
        patch("memos.embedders.factory.EmbedderFactory.from_config"),
        patch("memos.graph_dbs.factory.GraphStoreFactory.from_config"),
        patch("memos.memories.textual.tree_text_memory.organize.manager.MemoryManager"),
    ):
        instance = TreeTextMemory(mock_config)
        yield instance


def test_add_calls_manager(mock_tree_text_memory):
    mock_tree_text_memory.memory_manager.add = MagicMock()
    mock_item = TextualMemoryItem(
        id=str(uuid.uuid4()),
        memory="Test memory",
        metadata=TreeNodeTextualMemoryMetadata(updated_at=None),
    )
    mock_tree_text_memory.add([mock_item])
    mock_tree_text_memory.memory_manager.add.assert_called_once_with(
        [mock_item], user_name=None, mode="sync"
    )


def test_get_working_memory_sorted(mock_tree_text_memory):
    older = TextualMemoryItem(
        id=str(uuid.uuid4()),
        memory="Older",
        metadata=TreeNodeTextualMemoryMetadata(updated_at="2020-01-01"),
    )
    newer = TextualMemoryItem(
        id=str(uuid.uuid4()),
        memory="Newer",
        metadata=TreeNodeTextualMemoryMetadata(updated_at="2025-01-01"),
    )
    mock_tree_text_memory.graph_store.get_all_memory_items = MagicMock(
        return_value=[older.model_dump(), newer.model_dump()]
    )

    result = mock_tree_text_memory.get_working_memory()
    assert result[0].id == newer.id


def test_get_memory_found(mock_tree_text_memory):
    test_id = str(uuid.uuid4())
    fake_record = {"id": test_id, "memory": "Test", "metadata": {}}
    mock_tree_text_memory.graph_store.get_node = MagicMock(return_value=fake_record)

    memory = mock_tree_text_memory.get(test_id)
    assert memory.id == test_id


def test_get_memory_not_found(mock_tree_text_memory):
    mock_tree_text_memory.graph_store.get_node = MagicMock(return_value=None)
    with pytest.raises(ValueError):
        mock_tree_text_memory.get(str(uuid.uuid4()))


def test_delete_all(mock_tree_text_memory):
    mock_tree_text_memory.graph_store.clear = MagicMock()
    mock_tree_text_memory.delete_all()
    mock_tree_text_memory.graph_store.clear.assert_called_once()


def test_delete_propagates_failures_after_attempting_every_id(mock_tree_text_memory):
    failing_id = str(uuid.uuid4())
    succeeding_id = str(uuid.uuid4())

    def _delete_node(mid, user_name=None):
        if mid == failing_id:
            raise RuntimeError("db down")

    mock_tree_text_memory.graph_store.delete_node = MagicMock(side_effect=_delete_node)

    with pytest.raises(MemoryDeleteError, match="Failed to delete 1 of 2"):
        mock_tree_text_memory.delete([failing_id, succeeding_id], user_name="alice")

    # Best effort: every id is attempted, and the failure is not swallowed.
    assert mock_tree_text_memory.graph_store.delete_node.call_count == 2
    mock_tree_text_memory.graph_store.delete_node.assert_any_call(succeeding_id, user_name="alice")


def test_get_relevant_subgraph_embedding_recall_filters_activated_status(
    mock_tree_text_memory,
):
    mock_tree_text_memory.embedder.embed = MagicMock(return_value=[[0.1] * 5])
    mock_tree_text_memory.graph_store.search_by_embedding = MagicMock(return_value=[])

    result = mock_tree_text_memory.get_relevant_subgraph("hello", search_type="embedding")

    assert result == {"core_id": None, "nodes": [], "edges": []}
    kwargs = mock_tree_text_memory.graph_store.search_by_embedding.call_args.kwargs
    assert kwargs["status"] == "activated"


def test_get_relevant_subgraph_fulltext_recall_filters_activated_status(
    mock_tree_text_memory,
):
    mock_tree_text_memory.graph_store.search_by_fulltext = MagicMock(return_value=[])

    result = mock_tree_text_memory.get_relevant_subgraph("hello world", search_type="fulltext")

    assert result == {"core_id": None, "nodes": [], "edges": []}
    kwargs = mock_tree_text_memory.graph_store.search_by_fulltext.call_args.kwargs
    assert kwargs["status"] == "activated"


def test_load_file_not_exists(mock_tree_text_memory, tmp_path):
    mock_tree_text_memory.config.memory_filename = "memory.json"
    mock_tree_text_memory.graph_store.import_graph = MagicMock()

    result = tmp_path / "does_not_exist"
    mock_tree_text_memory.load(str(result))
    # Should log a warning but not raise


def test_dump_and_load_success(tmp_path, mock_tree_text_memory):
    mock_tree_text_memory.graph_store.export_graph = MagicMock(
        return_value={"nodes": [{"id": "1"}]}
    )
    mock_tree_text_memory.config.memory_filename = "memory.json"
    mock_tree_text_memory.dump(str(tmp_path))

    dumped_file = tmp_path / "memory.json"
    assert dumped_file.exists()


def test_drop_creates_backup_and_cleans(mock_tree_text_memory):
    mock_tree_text_memory.dump = MagicMock()
    mock_tree_text_memory._cleanup_old_backups = MagicMock()
    mock_tree_text_memory.graph_store.drop_database = MagicMock()

    mock_tree_text_memory.drop(keep_last_n=1)
    mock_tree_text_memory.dump.assert_called_once()
    mock_tree_text_memory._cleanup_old_backups.assert_called_once()
    mock_tree_text_memory.graph_store.drop_database.assert_called_once()


def test_add_returns_ids(mock_tree_text_memory):
    # Mock the memory_manager.add to return specific IDs
    dummy_ids = ["id1", "id2"]
    mock_tree_text_memory.memory_manager.add = MagicMock(return_value=dummy_ids)

    mock_items = [
        TextualMemoryItem(
            id=str(uuid.uuid4()),
            memory="Memory 1",
            metadata=TreeNodeTextualMemoryMetadata(updated_at=None),
        ),
        TextualMemoryItem(
            id=str(uuid.uuid4()),
            memory="Memory 2",
            metadata=TreeNodeTextualMemoryMetadata(updated_at=None),
        ),
    ]

    result = mock_tree_text_memory.add(mock_items)

    assert result == dummy_ids
    mock_tree_text_memory.memory_manager.add.assert_called_once_with(
        mock_items, user_name=None, mode="sync"
    )


def test_delete_by_memory_ids_passes_tenant_scope(mock_tree_text_memory):
    graph_store = MagicMock()
    mock_tree_text_memory.graph_store = graph_store

    mock_tree_text_memory.delete_by_memory_ids(["m_1"], writable_cube_ids=["cube_a"])

    graph_store.delete_node_by_prams.assert_called_once_with(
        memory_ids=["m_1"], writable_cube_ids=["cube_a"]
    )


def test_delete_by_memory_ids_does_not_swallow_failures(mock_tree_text_memory):
    graph_store = MagicMock()
    graph_store.delete_node_by_prams.side_effect = RuntimeError("graph down")
    mock_tree_text_memory.graph_store = graph_store

    with pytest.raises(RuntimeError, match="graph down"):
        mock_tree_text_memory.delete_by_memory_ids(["m_1"], writable_cube_ids=["cube_a"])
