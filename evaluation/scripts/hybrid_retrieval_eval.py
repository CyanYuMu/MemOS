#!/usr/bin/env python
"""Offline before/after evaluation for hybrid (vector + BM25) retrieval.

Why this script exists
----------------------
The repository ships LoCoMo dialogues with per-question evidence turn ids
(``D<session>:<turn>``).  That is enough to score *retrieval* without any LLM or
external embedding service, which makes the numbers reproducible in CI and on a
laptop.

Retrieval configurations compared
---------------------------------
* ``vector``      -- cosine similarity over a deterministic hashing embedding
                     (character 2-3 grams).  This is a *stand-in* for a learned
                     embedding: it needs no model download and no network, so the
                     harness stays reproducible.  Absolute numbers are therefore
                     not comparable to a sentence-transformer baseline.
* ``bm25``        -- lexical retrieval only (rank_bm25 + jieba/ngram tokenizer).
* ``hybrid-rrf``  -- Reciprocal Rank Fusion of the two rankings above.
* ``hybrid-weighted`` -- min-max normalised weighted fusion.
* ``bm25-rm3``     -- BM25 + RM3-lite pseudo-relevance feedback (``--rm3``).

Measured on LoCoMo (1977 questions), RM3-lite *hurts* precise questions
(Recall@5 0.4406 vs 0.5195 for plain BM25), so it stays opt-in and is not a
recommended default.  The flag exists so the negative result is reproducible.

Metrics (macro-averaged over questions; binary relevance)
---------------------------------------------------------
* Recall@k  -- ``|retrieved@k ∩ evidence| / |evidence|``.
* Hit@k     -- 1 if at least one evidence turn is in the top-k.
* MRR@k     -- reciprocal rank of the first evidence turn (0 if none in top-k).
* nDCG@k    -- ``DCG@k / IDCG@k`` with ``DCG = Σ rel_i / log2(i+1)``.

Each configuration retrieves the same number of documents; hybrid fusion uses
``candidate_top_k = max(k, 50)`` candidates per retriever before truncating.

Usage
-----
    PYTHONPATH=src python evaluation/scripts/hybrid_retrieval_eval.py \
        --data evaluation/data/locomo/locomo10.json --k 5 10 20

The script only reads data that is already in the repository; it never writes
datasets or credentials.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from memos.configs.search import HybridSearchConfig
from memos.search.hybrid import (
    BM25Index,
    CorpusDocument,
    HybridSearcher,
    is_bm25_available,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


@dataclass(frozen=True)
class Query:
    question: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Sample:
    sample_id: str
    documents: list[CorpusDocument]
    queries: list[Query]


def _session_sort_key(name: str) -> int:
    try:
        return int(name.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def load_samples(path: Path, limit: int | None = None) -> list[Sample]:
    with path.open(encoding="utf-8") as handle:
        raw_samples = json.load(handle)
    if limit is not None:
        raw_samples = raw_samples[:limit]

    samples: list[Sample] = []
    for raw in raw_samples:
        conversation = raw.get("conversation") or {}
        documents: list[CorpusDocument] = []
        for session_name in sorted(
            (key for key in conversation if key.startswith("session_") and "date" not in key),
            key=_session_sort_key,
        ):
            for turn in conversation.get(session_name) or []:
                dia_id = turn.get("dia_id")
                text = (turn.get("text") or "").strip()
                if not dia_id or not text:
                    continue
                speaker = turn.get("speaker") or ""
                documents.append(
                    CorpusDocument(
                        item_id=dia_id,
                        text=f"{speaker}: {text}" if speaker else text,
                    )
                )

        known_ids = {document.item_id for document in documents}
        queries: list[Query] = []
        for qa in raw.get("qa") or []:
            evidence = tuple(
                dict.fromkeys(
                    evidence_id
                    for evidence_id in (qa.get("evidence") or [])
                    if evidence_id in known_ids
                )
            )
            question = (qa.get("question") or "").strip()
            if evidence and question:
                queries.append(Query(question=question, evidence=evidence))

        if documents and queries:
            samples.append(
                Sample(
                    sample_id=str(raw.get("sample_id", len(samples))),
                    documents=documents,
                    queries=queries,
                )
            )
    return samples


class HashingVectorRetriever:
    """Deterministic stand-in embedding: character n-gram hashing + cosine."""

    def __init__(self, documents: Sequence[CorpusDocument]):
        self._documents = list(documents)
        self._vectorizer = HashingVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 3),
            n_features=2**18,
            alternate_sign=False,
            norm="l2",
        )
        self._matrix = self._vectorizer.transform([document.text for document in self._documents])

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if top_k <= 0 or not self._documents:
            return []
        query_vector = self._vectorizer.transform([query])
        similarities = cosine_similarity(query_vector, self._matrix)[0]
        order = similarities.argsort()[::-1][:top_k]
        return [(self._documents[index].item_id, float(similarities[index])) for index in order]


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    retrieved = set(ranked[:k])
    return len(retrieved & relevant_set) / len(relevant_set)


def hit_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    return 1.0 if relevant_set and set(ranked[:k]) & relevant_set else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    for position, item_id in enumerate(ranked[:k], start=1):
        if item_id in relevant_set:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, item_id in enumerate(ranked[:k], start=1)
        if item_id in relevant_set
    )
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_retrievers(
    sample: Sample,
    use_jieba: bool,
    vector_weight: float = 1.0,
    lexical_weight: float = 1.0,
    rm3: bool = False,
    rm3_feedback_docs: int = 3,
    rm3_max_terms: int = 10,
):
    vector = HashingVectorRetriever(sample.documents)
    bm25 = BM25Index(sample.documents, use_jieba=use_jieba)
    bm25_rm3 = (
        BM25Index(
            sample.documents,
            use_jieba=use_jieba,
            query_expansion="rm3",
            feedback_docs=rm3_feedback_docs,
            max_feedback_terms=rm3_max_terms,
        )
        if rm3
        else None
    )

    def vector_only(query: Query, limit: int) -> list[str]:
        return [item_id for item_id, _ in vector.search(query.question, limit)]

    def bm25_only(query: Query, limit: int) -> list[str]:
        return [item_id for item_id, _ in bm25.search(query.question, limit)]

    def bm25_expanded(query: Query, limit: int) -> list[str]:
        assert bm25_rm3 is not None
        return [item_id for item_id, _ in bm25_rm3.search(query.question, limit)]

    def hybrid(fusion: str, expansion: str = "none"):
        searcher = HybridSearcher(
            HybridSearchConfig(
                fusion=fusion,
                candidate_top_k=50,
                vector_weight=vector_weight,
                lexical_weight=lexical_weight,
                use_jieba=use_jieba,
                index_ttl_seconds=3600,
                query_expansion=expansion,
                rm3_feedback_docs=rm3_feedback_docs,
                rm3_max_terms=rm3_max_terms,
            )
        )

        def retrieve(query: Query, limit: int) -> list[str]:
            vector_hits = vector.search(query.question, searcher.candidate_top_k)
            hits = searcher.fuse(
                query.question,
                vector_hits,
                lambda: sample.documents,
                limit=limit,
            )
            return [hit.item_id for hit in hits or []]

        return retrieve

    retrievers = {
        "vector": vector_only,
        "bm25": bm25_only,
        "hybrid-rrf": hybrid("rrf"),
        "hybrid-weighted": hybrid("weighted"),
    }
    if rm3:
        retrievers["bm25-rm3"] = bm25_expanded
    return retrievers


def print_table(results: dict[str, dict[int, dict[str, float]]], k_values: Sequence[int]) -> None:
    header = f"{'method':<18}" + "".join(
        f"  R@{k:<5} Hit@{k:<5} MRR@{k:<5} nDCG@{k:<5}" for k in k_values
    )
    print(header)
    print("-" * len(header))
    for method, per_k in results.items():
        row = f"{method:<18}"
        for k in k_values:
            values = per_k[k]
            row += (
                f"  {values['recall']:<7.4f} {values['hit']:<7.4f} "
                f"{values['mrr']:<7.4f} {values['ndcg']:<7.4f}"
            )
        print(row)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("evaluation/data/locomo/locomo10.json"),
        help="LoCoMo-format JSON with conversation turns and evidence ids.",
    )
    parser.add_argument("--k", type=int, nargs="+", default=[5, 10, 20])
    parser.add_argument("--samples", type=int, default=None, help="Limit the number of samples.")
    parser.add_argument(
        "--no-jieba",
        action="store_true",
        help="Use the n-gram fallback instead of jieba (useful for dependency checks).",
    )
    parser.add_argument("--vector-weight", type=float, default=1.0)
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument(
        "--rm3",
        action="store_true",
        help="Also evaluate BM25 with RM3-lite pseudo-relevance feedback.",
    )
    parser.add_argument("--rm3-feedback-docs", type=int, default=3)
    parser.add_argument("--rm3-max-terms", type=int, default=10)
    args = parser.parse_args(argv)

    if not is_bm25_available():
        print(
            "rank-bm25 is required: pip install 'MemoryOS[hybrid-search]'",
            file=sys.stderr,
        )
        return 2

    samples = load_samples(args.data, limit=args.samples)
    if not samples:
        print(f"No usable samples in {args.data}", file=sys.stderr)
        return 1

    queries = sum(len(sample.queries) for sample in samples)
    print(f"data={args.data} samples={len(samples)} queries={queries} jieba={not args.no_jieba}")

    method_names = ["vector", "bm25", "hybrid-rrf", "hybrid-weighted"]
    if args.rm3:
        method_names.append("bm25-rm3")
    per_method: dict[str, dict[int, dict[str, list[float]]]] = {
        method: {k: {"recall": [], "hit": [], "mrr": [], "ndcg": []} for k in args.k}
        for method in method_names
    }

    # Build requesters/indexes once per sample: the vector matrix and BM25 index
    # are reused for every question, and across all four methods.
    for sample in samples:
        retrievers = build_retrievers(
            sample,
            use_jieba=not args.no_jieba,
            vector_weight=args.vector_weight,
            lexical_weight=args.lexical_weight,
            rm3=args.rm3,
            rm3_feedback_docs=args.rm3_feedback_docs,
            rm3_max_terms=args.rm3_max_terms,
        )
        for query in sample.queries:
            for method in method_names:
                ranked = retrievers[method](query, max(args.k))
                buckets = per_method[method]
                for k in args.k:
                    buckets[k]["recall"].append(recall_at_k(ranked, query.evidence, k))
                    buckets[k]["hit"].append(hit_at_k(ranked, query.evidence, k))
                    buckets[k]["mrr"].append(reciprocal_rank(ranked, query.evidence, k))
                    buckets[k]["ndcg"].append(ndcg_at_k(ranked, query.evidence, k))

    results: dict[str, dict[int, dict[str, float]]] = {
        method: {
            k: {metric: _mean(values) for metric, values in per_metric.items()}
            for k, per_metric in buckets.items()
        }
        for method, buckets in per_method.items()
    }

    print_table(results, args.k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
