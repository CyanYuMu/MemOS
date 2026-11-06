"""Integration tests for hybrid retrieval inside ``GeneralTextMemory``."""

from __future__ import annotations

import uuid

from unittest.mock import MagicMock, patch

import pytest

from memos.configs.embedder import EmbedderConfigFactory
from memos.configs.llm import LLMConfigFactory
from memos.configs.memory import GeneralTextMemoryConfig
from memos.configs.search import HybridSearchConfig
from memos.configs.vec_db import VectorDBConfigFactory
from memos.embedders.factory import OllamaEmbedder
from memos.exceptions import ConfigurationError
from memos.llms.factory import OllamaLLM
from memos.memories.textual.general import GeneralTextMemory
from memos.memories.textual.item import TextualMemoryItem
from memos.vec_dbs.factory import QdrantVecDB
from memos.vec_dbs.item import VecDBItem


pytest.importorskip("rank_bm25")


def _db_item(text: str, score: float, item_id: str | None = None) -> VecDBItem:
    item_id = item_id or str(uuid.uuid4())
    return VecDBItem(
        id=item_id,
        vector=[0.1, 0.2],
        payload={
            "id": item_id,
            "memory": text,
            "metadata": {"source": "conversation", "key": text[:16]},
        },
        score=score,
    )


@pytest.fixture
def memory_parts():
    """Build a ``GeneralTextMemory`` with mocked providers."""
    llm_config = MagicMock(spec=LLMConfigFactory)
    llm_config.backend = "ollama"
    llm_config.config = {"model_name_or_path": "test-llm"}
    llm_config.model_schema = "memos.configs.llm.LLMConfigFactory"
    embedder_config = MagicMock(spec=EmbedderConfigFactory)
    embedder_config.backend = "ollama"
    embedder_config.config = {"model_name_or_path": "test-embedder"}
    embedder_config.model_schema = "memos.configs.embedder.EmbedderConfigFactory"
    vector_config = MagicMock(spec=VectorDBConfigFactory)
    vector_config.backend = "qdrant"
    vector_config.config = {"collection_name": "test-hybrid"}
    vector_config.model_schema = "memos.configs.vec_db.VectorDBConfigFactory"

    run_config = MagicMock()
    run_config.collection_name = "test-hybrid"

    llm = MagicMock(spec=OllamaLLM)
    embedder = MagicMock(spec=OllamaEmbedder)
    vector_db = MagicMock(spec=QdrantVecDB)
    vector_db.config = run_config

    with (
        patch("memos.memories.textual.general.LLMFactory") as llm_factory,
        patch("memos.memories.textual.general.EmbedderFactory") as embedder_factory,
        patch("memos.memories.textual.general.VecDBFactory") as vec_factory,
    ):
        llm_factory.from_config.return_value = llm
        embedder_factory.from_config.return_value = embedder
        vec_factory.from_config.return_value = vector_db

        def build(hybrid: HybridSearchConfig | None) -> GeneralTextMemory:
            config = GeneralTextMemoryConfig(
                extractor_llm=llm_config,
                embedder=embedder_config,
                vector_db=vector_config,
                hybrid_search=hybrid,
            )
            return GeneralTextMemory(config)

        yield build, vector_db, embedder


def _build(build, hybrid):
    memory = build(hybrid)
    return memory


class TestHybridDisabledEquivalence:
    def test_unset_config_keeps_vector_only_call(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, None)
        embedder.embed.return_value = [[0.4, 0.5]]
        vector_db.search.return_value = [_db_item("alpha", 0.9)]

        memory.search("alpha", top_k=3)

        vector_db.search.assert_called_once_with([0.4, 0.5], 3)
        vector_db.get_all.assert_not_called()

    def test_enabled_false_keeps_vector_only_call(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(enabled=False))
        embedder.embed.return_value = [[0.4, 0.5]]
        vector_db.search.return_value = [_db_item("alpha", 0.9)]

        memory.search("alpha", top_k=3)

        vector_db.search.assert_called_once_with([0.4, 0.5], 3)
        vector_db.get_all.assert_not_called()

    def test_missing_rank_bm25_degrades_to_vector_only(self, memory_parts):
        build, vector_db, embedder = memory_parts
        embedder.embed.return_value = [[0.4, 0.5]]
        vector_db.search.return_value = [_db_item("alpha", 0.9)]

        with patch("memos.search.hybrid.is_bm25_available", return_value=False):
            memory = _build(build, HybridSearchConfig(candidate_top_k=10))

        memory.search("alpha", top_k=3)

        vector_db.search.assert_called_once_with([0.4, 0.5], 3)
        vector_db.get_all.assert_not_called()


class TestHybridFusion:
    def test_lexical_only_hit_is_returned_and_deduped(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(
            build,
            HybridSearchConfig(candidate_top_k=10, lexical_weight=2.0, index_ttl_seconds=0),
        )
        embedder.embed.return_value = [[0.4, 0.5]]

        vector_hit = _db_item("unrelated weather note", 0.9)
        lexical_hit = _db_item("we migrated PostgreSQL to version 16", 0.1)
        vector_db.search.return_value = [vector_hit]
        vector_db.get_all.return_value = [vector_hit, lexical_hit]

        results = memory.search("PostgreSQL", top_k=2)

        assert [item.id for item in results] == [lexical_hit.id, vector_hit.id]
        assert len({item.id for item in results}) == 2
        vector_db.search.assert_called_once_with([0.4, 0.5], 10)

    def test_overlapping_hit_appears_once(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10))
        embedder.embed.return_value = [[0.4, 0.5]]

        shared = uuid.uuid4()
        shared_item = _db_item("PostgreSQL migration details", 0.9, str(shared))
        other = _db_item("weather", 0.1)
        vector_db.search.return_value = [shared_item, other]
        vector_db.get_all.return_value = [shared_item, other]

        results = memory.search("PostgreSQL migration", top_k=5)

        ids = [item.id for item in results]
        assert ids.count(str(shared)) == 1
        assert len(ids) == len(set(ids))

    def test_results_respect_top_k(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10))
        embedder.embed.return_value = [[0.4, 0.5]]

        items = [_db_item(f"PostgreSQL note {i}", 1.0 - i / 10) for i in range(6)]
        vector_db.search.return_value = items
        vector_db.get_all.return_value = items

        assert len(memory.search("PostgreSQL", top_k=2)) == 2

    def test_top_k_zero_returns_empty(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10))
        embedder.embed.return_value = [[0.4, 0.5]]
        vector_db.search.return_value = [_db_item("PostgreSQL", 0.9)]
        vector_db.get_all.return_value = vector_db.search.return_value

        assert memory.search("PostgreSQL", top_k=0) == []


class TestHybridRobustness:
    def test_empty_query_does_not_raise(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10))
        embedder.embed.return_value = [[0.4, 0.5]]
        item = _db_item("anything", 0.9)
        vector_db.search.return_value = [item]
        vector_db.get_all.return_value = [item]

        results = memory.search("", top_k=3)

        assert [result.id for result in results] == [item.id]

    def test_oversized_query_does_not_raise(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10, max_query_tokens=5))
        embedder.embed.return_value = [[0.4, 0.5]]
        item = _db_item("word", 0.9)
        vector_db.search.return_value = [item]
        vector_db.get_all.return_value = [item]

        assert memory.search("word " * 5_000, top_k=1)

    def test_corpus_snapshot_failure_falls_back(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10))
        embedder.embed.return_value = [[0.4, 0.5]]
        item = _db_item("PostgreSQL", 0.9)
        vector_db.search.return_value = [item]
        vector_db.get_all.side_effect = RuntimeError("store unavailable")

        results = memory.search("PostgreSQL", top_k=3)

        assert [result.id for result in results] == [item.id]

    def test_writes_invalidate_the_index(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10, index_ttl_seconds=3600))
        embedder.embed.return_value = [[0.4, 0.5]]
        item = _db_item("PostgreSQL", 0.9)
        vector_db.search.return_value = [item]
        vector_db.get_all.return_value = [item]

        memory.search("PostgreSQL", top_k=1)
        assert vector_db.get_all.call_count == 1

        # A write must force a rebuild even though the TTL has not elapsed.
        embedder.embed.return_value = [[0.6, 0.7]]
        memory.add([TextualMemoryItem(id=str(uuid.uuid4()), memory="new note", metadata={})])
        embedder.embed.return_value = [[0.4, 0.5]]

        memory.search("PostgreSQL", top_k=1)
        assert vector_db.get_all.call_count == 2


class TestHybridSnapshotContract:
    """The lexical index must not depend on pulling embeddings for the corpus."""

    def test_snapshot_incapable_backend_is_rejected(self, memory_parts):
        build, vector_db, _ = memory_parts
        vector_db.supports_text_snapshot = False

        with pytest.raises(ConfigurationError, match="hybrid_search requires"):
            build(HybridSearchConfig())

    def test_snapshot_requests_payloads_only(self, memory_parts):
        build, vector_db, embedder = memory_parts
        memory = _build(build, HybridSearchConfig(candidate_top_k=10, index_ttl_seconds=0))
        embedder.embed.return_value = [[0.4, 0.5]]
        item = _db_item("PostgreSQL", 0.9)
        vector_db.search.return_value = [item]
        vector_db.get_all.return_value = [item]

        memory.search("PostgreSQL", top_k=1)

        vector_db.get_all.assert_called_once_with(with_vectors=False)

    def test_snapshot_falls_back_to_original_text(self, memory_parts):
        build, vector_db, _ = memory_parts
        memory = _build(build, HybridSearchConfig())
        item = VecDBItem(
            id=str(uuid.uuid4()), vector=None, payload={"original_text": "PostgreSQL 16"}
        )
        vector_db.get_all.return_value = [item]

        documents = memory._hybrid_documents()

        assert [(doc.item_id, doc.text) for doc in documents] == [(item.id, "PostgreSQL 16")]
