"""Unit tests for the hybrid (BM25 + vector) retrieval primitives."""

from __future__ import annotations

import math

from unittest.mock import patch

import pytest

from memos.configs.search import HybridSearchConfig
from memos.search import text_tokenizer
from memos.search.hybrid import (
    BM25Index,
    CorpusDocument,
    HybridSearcher,
    reciprocal_rank_fusion,
    weighted_fusion,
)
from memos.search.text_tokenizer import tokenize


pytest.importorskip("rank_bm25")


DOCUMENTS = [
    CorpusDocument("d1", "I work in Beijing on database systems", {"memory": "d1"}),
    CorpusDocument("d2", "My favourite colour is blue", {"memory": "d2"}),
    CorpusDocument("d3", "北京是中国的首都，数据库工程师很多", {"memory": "d3"}),
]


class TestTokenizer:
    def test_chinese_is_segmented_into_words(self):
        tokens = tokenize("我喜欢数据库和分布式系统")
        assert "数据库" in tokens
        assert "分布式系统" in tokens
        assert all(isinstance(token, str) and token for token in tokens)

    def test_english_is_lowercased_and_punctuated_away(self):
        assert tokenize("Hello, WORLD! It's a Test.") == ["hello", "world", "test"]

    def test_snake_case_keeps_whole_and_parts(self):
        tokens = tokenize("user_name column")
        assert tokens[0] == "user_name"
        assert "user" in tokens
        assert "name" in tokens

    def test_stopwords_are_removed(self):
        assert tokenize("the quick brown fox") == ["quick", "brown", "fox"]
        assert "的" not in tokenize("我的数据库")

    def test_extra_stopwords_are_merged(self):
        assert tokenize("quick brown fox", stopwords=["quick"]) == ["brown", "fox"]

    def test_empty_and_non_string_inputs(self):
        assert tokenize(None) == []
        assert tokenize("") == []
        assert tokenize("   ") == []
        assert tokenize(123) == []  # type: ignore[arg-type]

    def test_max_tokens_truncates(self):
        assert len(tokenize("one two three four five", max_tokens=2)) == 2
        assert tokenize("one two", max_tokens=0) == []

    def test_ngram_fallback_without_jieba(self):
        with patch.object(text_tokenizer, "_get_jieba", return_value=None):
            tokens = tokenize("北京欢迎你")
        assert "北京" in tokens
        assert "欢迎" in tokens
        assert "北京欢迎你" not in tokens


class TestReciprocalRankFusion:
    def test_fuses_and_dedupes_across_rankings(self):
        fused = reciprocal_rank_fusion([["a", "b"], ["b", "a", "c"]], k=60)
        ids = [item_id for item_id, _ in fused]
        assert len(ids) == len(set(ids)) == 3
        # "a" is 1st and 2nd; "b" is 2nd and 1st -> tie broken by insertion order.
        assert set(ids[:2]) == {"a", "b"}
        assert ids[2] == "c"

    def test_duplicate_inside_one_ranking_counts_once(self):
        fused = dict(reciprocal_rank_fusion([["a", "a", "b"]], k=0))
        assert fused["a"] == pytest.approx(1.0)
        assert fused["b"] == pytest.approx(0.5)

    def test_weights_are_applied(self):
        fused = dict(reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0, 0.0], k=0))
        assert "a" in fused
        assert "b" not in fused

    def test_empty_rankings(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[], []]) == []

    def test_limit(self):
        assert len(reciprocal_rank_fusion([["a", "b", "c"]], limit=2)) == 2

    def test_weight_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])


class TestWeightedFusion:
    def test_scores_are_normalised_before_combining(self):
        fused = dict(
            weighted_fusion(
                [[("a", 1.0), ("b", 0.0)], [("b", 100.0), ("c", 50.0)]],
                weights=[1.0, 1.0],
            )
        )
        # "b" is max on the lexical side and min on the vector side.
        assert fused["a"] == pytest.approx(1.0)
        assert fused["b"] == pytest.approx(1.0)
        assert fused["c"] == pytest.approx(0.0)

    def test_non_finite_scores_are_ignored(self):
        fused = dict(weighted_fusion([[("a", math.nan), ("b", math.inf), ("c", 1.0)]]))
        assert fused == {"c": 1.0}

    def test_empty_input(self):
        assert weighted_fusion([]) == []
        assert weighted_fusion([[]]) == []


class TestBM25Index:
    def test_english_query(self):
        index = BM25Index(DOCUMENTS)
        hits = index.search("database systems", top_k=5)
        assert hits
        assert hits[0][0] == "d1"

    def test_chinese_query(self):
        index = BM25Index(DOCUMENTS)
        hits = index.search("数据库", top_k=5)
        ids = [item_id for item_id, _ in hits]
        assert "d3" in ids
        assert "d2" not in ids

    def test_unmatched_query_returns_nothing(self):
        index = BM25Index(DOCUMENTS)
        assert index.search("zzzzzz", top_k=5) == []

    def test_empty_query_and_zero_top_k(self):
        index = BM25Index(DOCUMENTS)
        assert index.search("", top_k=5) == []
        assert index.search("database", top_k=0) == []

    def test_empty_corpus_and_blank_documents(self):
        assert BM25Index([]).search("anything", top_k=5) == []
        blank = BM25Index([CorpusDocument("d1", "   "), CorpusDocument("d2", "")])
        assert blank.search("anything", top_k=5) == []

    def test_rm3_is_off_by_default(self):
        plain = BM25Index(DOCUMENTS)
        explicit = BM25Index(DOCUMENTS, query_expansion="none")
        assert plain.search("database systems", top_k=5) == explicit.search(
            "database systems", top_k=5
        )

    def test_rm3_adds_pseudo_relevant_documents(self):
        docs = [
            CorpusDocument("d1", "alpha beta gamma"),
            CorpusDocument("d2", "beta delta"),
        ]
        plain = BM25Index(docs)
        expanded = BM25Index(docs, query_expansion="rm3", feedback_docs=1, max_feedback_terms=5)
        # "beta" only exists outside the original match, so it can only be
        # recalled through the feedback pass.
        assert [item_id for item_id, _ in plain.search("alpha", top_k=5)] == ["d1"]
        assert "d2" in [item_id for item_id, _ in expanded.search("alpha", top_k=5)]

    def test_rm3_without_new_terms_keeps_first_pass(self):
        docs = [CorpusDocument("d1", "alpha"), CorpusDocument("d2", "alpha")]
        expanded = BM25Index(docs, query_expansion="rm3", feedback_docs=3)
        assert [item_id for item_id, _ in expanded.search("alpha", top_k=5)] == ["d1", "d2"]

    def test_scores_are_finite_and_sorted(self):
        hits = BM25Index(DOCUMENTS).search("Beijing database", top_k=5)
        scores = [score for _, score in hits]
        assert all(math.isfinite(score) for score in scores)
        assert scores == sorted(scores, reverse=True)

    def test_tiny_corpus_zero_idf_match_is_kept(self):
        # "postgresql" appears in half of a two-document corpus, so BM25's IDF
        # is exactly zero; the match must still be reported.
        index = BM25Index(
            [
                CorpusDocument("a", "unrelated weather note"),
                CorpusDocument("b", "we migrated PostgreSQL"),
            ]
        )
        assert [item_id for item_id, _ in index.search("PostgreSQL", top_k=5)] == ["b"]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestHybridSearcher:
    def test_disabled_by_configuration(self):
        searcher = HybridSearcher(HybridSearchConfig(enabled=False))
        assert not searcher.enabled
        assert searcher.fuse("database", [("d1", 0.5)], lambda: DOCUMENTS) is None

    def test_disabled_when_both_weights_are_zero(self):
        searcher = HybridSearcher(HybridSearchConfig(vector_weight=0.0, lexical_weight=0.0))
        assert not searcher.enabled

    def test_missing_rank_bm25_degrades_to_disabled(self):
        with patch("memos.search.hybrid.is_bm25_available", return_value=False):
            searcher = HybridSearcher(HybridSearchConfig())
        assert not searcher.enabled
        assert "rank-bm25" in (searcher.disabled_reason or "")

    def test_fuse_returns_fused_hits_with_ranks(self):
        searcher = HybridSearcher(HybridSearchConfig(candidate_top_k=10, lexical_weight=2.0))
        hits = searcher.fuse(
            "数据库",
            [("d2", 0.9), ("d1", 0.8)],
            lambda: DOCUMENTS,
            limit=3,
        )
        assert hits is not None
        assert hits[0].item_id == "d3"  # lexical-only hit rises above vector hits
        by_id = {hit.item_id: hit for hit in hits}
        assert by_id["d3"].vector_rank is None
        assert by_id["d3"].lexical_rank == 1
        assert by_id["d3"].payload == {"memory": "d3"}

    def test_weighted_mode(self):
        searcher = HybridSearcher(
            HybridSearchConfig(fusion="weighted", candidate_top_k=10, lexical_weight=2.0)
        )
        hits = searcher.fuse("database", [("d2", 0.9)], lambda: DOCUMENTS)
        assert hits is not None
        assert hits[0].item_id == "d1"

    def test_empty_query_returns_vector_order(self):
        searcher = HybridSearcher(HybridSearchConfig(candidate_top_k=10))
        hits = searcher.fuse("", [("d2", 0.9), ("d1", 0.8)], lambda: DOCUMENTS)
        assert hits is not None
        assert [hit.item_id for hit in hits[:2]] == ["d2", "d1"]

    def test_oversized_query_is_bounded(self):
        searcher = HybridSearcher(HybridSearchConfig(candidate_top_k=10, max_query_tokens=3))
        hits = searcher.fuse("word " * 10_000, [("d1", 1.0)], lambda: DOCUMENTS)
        assert hits is not None

    def test_corpus_provider_failure_falls_back(self):
        searcher = HybridSearcher(HybridSearchConfig())

        def boom():
            raise RuntimeError("vector store unavailable")

        assert searcher.fuse("database", [("d1", 1.0)], boom) is None

    def test_empty_corpus_keeps_vector_results(self):
        searcher = HybridSearcher(HybridSearchConfig())
        hits = searcher.fuse("database", [("d1", 1.0)], list)
        assert hits is not None
        assert [hit.item_id for hit in hits] == ["d1"]

    def test_index_is_cached_until_invalidated(self):
        clock = FakeClock()
        searcher = HybridSearcher(HybridSearchConfig(index_ttl_seconds=60), clock=clock)
        calls = {"count": 0}

        def provider():
            calls["count"] += 1
            return DOCUMENTS

        searcher.fuse("database", [("d1", 1.0)], provider)
        searcher.fuse("database", [("d1", 1.0)], provider)
        assert calls["count"] == 1

        clock.now = 61.0
        searcher.fuse("database", [("d1", 1.0)], provider)
        assert calls["count"] == 2

        searcher.invalidate()
        searcher.fuse("database", [("d1", 1.0)], provider)
        assert calls["count"] == 3

    def test_zero_ttl_rebuilds_every_search(self):
        searcher = HybridSearcher(HybridSearchConfig(index_ttl_seconds=0))
        calls = {"count": 0}

        def provider():
            calls["count"] += 1
            return DOCUMENTS

        searcher.fuse("database", [("d1", 1.0)], provider)
        searcher.fuse("database", [("d1", 1.0)], provider)
        assert calls["count"] == 2
