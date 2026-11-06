from __future__ import annotations

import inspect

from memos.dream.pipeline.persistence import DreamPersistence
from memos.dream.types import (
    DreamAction,
    DreamActionType,
    DreamDiaryEntry,
    DreamResult,
    DreamSignalSnapshot,
    TargetMemoryType,
)
from memos.graph_dbs.base import BaseGraphDB
from memos.memories.textual.item import TreeNodeTextualMemoryMetadata
from memos.vec_dbs.item import VecDBItem


class FakeGraphDB:
    def __init__(self):
        self.added: list[tuple[str, str, dict, str | None]] = []
        self.updated: list[tuple[str, dict, str | None]] = []

    def add_node(self, id, memory, metadata, user_name=None):
        self.added.append((id, memory, metadata, user_name))

    def update_node(self, id, fields, user_name=None):
        self.updated.append((id, fields, user_name))

    def get_node(self, id, user_name=None):
        return None


class FakeEmbedder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


class BrokenEmbedder:
    def embed(self, texts):
        raise RuntimeError("embedding backend down")


def _action(**overrides) -> DreamAction:
    fields = {
        "action_type": DreamActionType.CREATE,
        "target_memory_type": TargetMemoryType.INSIGHT,
        "new_content": "Dream consolidated insight about the MemOS pipeline.",
        "rationale": "Answers how the Dream pipeline persists memories.",
        "confidence": 0.9,
    }
    fields.update(overrides)
    return DreamAction(**fields)


def _persistence(graph, embedder) -> DreamPersistence:
    persistence = DreamPersistence()
    shared: dict = {"graph_db": graph}
    if embedder is not None:
        shared["embedder"] = embedder
    persistence.bind_context({"shared": shared})
    return persistence


def _persist(persistence, actions=(), diary_entry=None) -> None:
    persistence.persist(
        results=[DreamResult(cluster_id="c1", actions=list(actions), diary_entry=diary_entry)],
        text_mem=object(),
        cube_id="cube-a",
        mem_cube_id="cube-a",
        user_id="u1",
        signal_snapshot=DreamSignalSnapshot(mem_cube_id="cube-a"),
    )


def _assert_vector_syncable(node_id: str, embedding) -> None:
    """Replay the exact call graph_db.add_node makes for the vector DB.

    A node whose id fails here is silently dropped from the vector index and
    can never be retrieved, no matter what its metadata says.
    """
    VecDBItem(id=node_id, vector=embedding, payload={"memory": "x", "vector_sync": "success"})


def test_every_persisted_dream_node_is_vector_syncable():
    graph = FakeGraphDB()
    embedder = FakeEmbedder()
    persistence = _persistence(graph, embedder)

    _persist(
        persistence,
        actions=[
            _action(),
            _action(action_type=DreamActionType.MERGE, source_memory_ids=["m1", "m2"]),
        ],
        diary_entry=DreamDiaryEntry(title="Dream", summary="A night of consolidation."),
    )

    assert len(graph.added) == 3
    for node_id, memory, metadata, user_name in graph.added:
        _assert_vector_syncable(node_id, metadata["embedding"])
        assert metadata["embedding"] is not None
        assert user_name == "cube-a"
        assert memory


def test_persisted_node_ids_are_unique_and_dashed_uuids():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[_action(), _action()],
        diary_entry=DreamDiaryEntry(title="Dream", summary="Summary"),
    )

    ids = [node_id for node_id, _memory, _metadata, _user_name in graph.added]
    assert len(set(ids)) == len(ids)
    for node_id in ids:
        assert "-" in node_id


def test_created_insight_embeds_its_own_content():
    graph = FakeGraphDB()
    embedder = FakeEmbedder()
    persistence = _persistence(graph, embedder)

    _persist(persistence, actions=[_action(new_content="Only this text should be embedded.")])

    node_id, _memory, metadata, _user_name = graph.added[0]
    assert embedder.calls == [["Only this text should be embedded."]]
    assert metadata["embedding"] == [float(len("Only this text should be embedded.")), 1.0]
    _assert_vector_syncable(node_id, metadata["embedding"])


def test_merged_insight_embeds_its_consolidated_content():
    graph = FakeGraphDB()
    embedder = FakeEmbedder()
    persistence = _persistence(graph, embedder)

    _persist(
        persistence,
        actions=[
            _action(
                action_type=DreamActionType.MERGE,
                new_content="Consolidated text.",
                source_memory_ids=["m1", "m2"],
            )
        ],
    )

    merged_id, _memory, metadata, _user_name = graph.added[0]
    assert embedder.calls == [["Consolidated text."]]
    assert metadata["merged_from"] == ["m1", "m2"]
    _assert_vector_syncable(merged_id, metadata["embedding"])


def test_diary_node_embeds_its_formatted_content():
    graph = FakeGraphDB()
    embedder = FakeEmbedder()
    persistence = _persistence(graph, embedder)
    entry = DreamDiaryEntry(title="Dream title", summary="Dream summary.")

    _persist(persistence, diary_entry=entry)

    node_id, memory, metadata, user_name = graph.added[0]
    assert embedder.calls == [[memory]]
    assert memory == entry.format_content()
    _assert_vector_syncable(node_id, metadata["embedding"])
    assert user_name == "cube-a"


def test_nodes_are_still_written_when_no_embedder_is_available():
    graph = FakeGraphDB()
    persistence = _persistence(graph, None)

    _persist(
        persistence,
        actions=[_action()],
        diary_entry=DreamDiaryEntry(title="Dream", summary="Summary"),
    )

    assert len(graph.added) == 2
    for node_id, _memory, metadata, _user_name in graph.added:
        assert metadata["embedding"] is None
        assert node_id


def test_embedding_failure_does_not_drop_the_node():
    graph = FakeGraphDB()
    persistence = _persistence(graph, BrokenEmbedder())

    _persist(persistence, actions=[_action()])

    assert len(graph.added) == 1
    assert graph.added[0][2]["embedding"] is None


def test_embed_text_prefers_the_shared_embedder():
    from memos.dream.embedding import embed_text

    shared_embedder = FakeEmbedder()
    text_mem = type("TextMem", (), {"embedder": FakeEmbedder()})()

    assert embed_text({"shared": {"embedder": shared_embedder, "text_mem": text_mem}}, "hi") == [
        2.0,
        1.0,
    ]
    assert shared_embedder.calls == [["hi"]]


def test_embed_text_falls_back_to_the_text_memory_embedder():
    from memos.dream.embedding import embed_text

    text_mem = type("TextMem", (), {"embedder": FakeEmbedder()})()

    assert embed_text({"shared": {"text_mem": text_mem}}, "hi") == [2.0, 1.0]


def test_embed_text_returns_none_without_embedder_or_text():
    from memos.dream.embedding import embed_text

    assert embed_text({"shared": {}}, "hi") is None
    assert embed_text(None, "hi") is None
    assert embed_text({"shared": {"embedder": FakeEmbedder()}}, "") is None


def test_embed_text_swallows_embedder_errors():
    from memos.dream.embedding import embed_text

    assert embed_text({"shared": {"embedder": BrokenEmbedder()}}, "hi") is None


def test_fake_graph_db_mirrors_the_real_update_node_signature():
    """The fake must not be more forgiving than the backend it stands in for.

    Dream's UPDATE/ARCHIVE/MERGE paths once passed (id, content, metadata,
    user_name=...) to a method declared as (id, fields, user_name=None); the
    TypeError was swallowed by a bare except, so those writes silently never
    happened. Keeping the fake's signature aligned is what catches that.
    """
    fake = inspect.signature(FakeGraphDB.update_node)
    real = inspect.signature(BaseGraphDB.update_node)

    assert [(p.name, p.kind) for p in fake.parameters.values()] == [
        (p.name, p.kind) for p in real.parameters.values()
    ]


def test_update_action_rewrites_the_target_memory_content():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[
            _action(
                action_type=DreamActionType.UPDATE,
                target_memory_id="mem-1",
                new_content="Dream-refined content.",
            )
        ],
    )

    assert len(graph.updated) == 1
    node_id, fields, user_name = graph.updated[0]
    assert node_id == "mem-1"
    assert fields["memory"] == "Dream-refined content."
    assert fields["dream_action_type"] == "update"
    assert user_name == "cube-a"
    assert graph.added == []


def test_update_action_without_new_content_keeps_the_stored_memory():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[
            _action(action_type=DreamActionType.UPDATE, target_memory_id="mem-1", new_content="")
        ],
    )

    node_id, fields, _user_name = graph.updated[0]
    assert node_id == "mem-1"
    assert "memory" not in fields
    assert fields["last_dream_update"]


def test_archive_action_marks_the_target_memory_archived():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[_action(action_type=DreamActionType.ARCHIVE, target_memory_id="mem-1")],
    )

    assert len(graph.updated) == 1
    node_id, fields, user_name = graph.updated[0]
    assert node_id == "mem-1"
    assert fields["status"] == "archived"
    assert fields["archive_reason"] == "Answers how the Dream pipeline persists memories."
    assert user_name == "cube-a"
    assert graph.added == []


def test_merge_action_archives_every_source_memory():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[
            _action(
                action_type=DreamActionType.MERGE,
                source_memory_ids=["m1", "m2"],
                new_content="Consolidated text.",
            )
        ],
    )

    merged_id = graph.added[0][0]
    assert [node_id for node_id, _fields, _user_name in graph.updated] == ["m1", "m2"]
    for _node_id, fields, user_name in graph.updated:
        # "merged" is not a valid node status, so a source node written with it
        # cannot be parsed back into a TextualMemoryItem.
        assert fields["status"] == "archived"
        assert fields["merged_into"] == merged_id
        assert user_name == "cube-a"
        TreeNodeTextualMemoryMetadata(status=fields["status"])


def test_actions_without_a_target_id_never_reach_the_graph():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())

    _persist(
        persistence,
        actions=[
            _action(action_type=DreamActionType.UPDATE),
            _action(action_type=DreamActionType.ARCHIVE),
        ],
    )

    assert graph.updated == []
    assert graph.added == []


def test_diary_metadata_carries_a_readable_task_id():
    graph = FakeGraphDB()
    persistence = _persistence(graph, FakeEmbedder())
    entry = DreamDiaryEntry(title="Dream", summary="Summary")

    _persist(persistence, diary_entry=entry)

    node_id, _memory, metadata, _user_name = graph.added[0]
    assert metadata["task_id"] == entry.task_id
    assert metadata["task_id"].startswith("dream_diary_")
    assert metadata["task_id"] != node_id
