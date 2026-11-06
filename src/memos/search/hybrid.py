"""Hybrid retrieval: BM25 lexical recall fused with the existing vector recall.

The module is intentionally self-contained and dependency-light:

* ``rank_bm25`` is imported lazily.  If it is not installed the hybrid searcher
  reports itself as disabled and callers keep their previous vector-only
  behaviour.
* Fusion never depends on the vector database: callers pass already-retrieved
  vector hits in, and the lexical side is built from a corpus provider.
* All public helpers are defensive about empty/oversized/non-string input;
  retrieval failures degrade to ``None`` (caller falls back) instead of bubbling
  up into the search path.
"""

from __future__ import annotations

import math
import threading
import time

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from memos.log import get_logger
from memos.search.text_tokenizer import tokenize


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from memos.configs.search import HybridSearchConfig


logger = get_logger(__name__)

_bm25_checked = False
_bm25_available = False


def is_bm25_available() -> bool:
    """Return whether ``rank_bm25`` can be imported (checked once)."""
    global _bm25_checked, _bm25_available
    if not _bm25_checked:
        _bm25_checked = True
        try:
            import rank_bm25  # noqa: F401 - intentionally lazy

            _bm25_available = True
        except ImportError:
            _bm25_available = False
    return _bm25_available


@dataclass(frozen=True)
class CorpusDocument:
    """A single item in the lexical index."""

    item_id: str
    text: str
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class HybridHit:
    """One fused result, with rank provenance for diagnostics."""

    item_id: str
    score: float
    vector_rank: int | None = None
    lexical_rank: int | None = None
    payload: dict[str, Any] | None = None


def _dedupe_ranking(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ranking: list[str] = []
    for item_id in ids:
        if item_id and item_id not in seen:
            seen.add(item_id)
            ranking.append(item_id)
    return ranking


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    weights: Sequence[float] | None = None,
    k: int = 60,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists with Reciprocal Rank Fusion (Cormack et al.).

    ``score(d) = sum_i weight_i / (k + rank_i(d))`` where ranks start at 1 and a
    document missing from a ranking contributes nothing.  Duplicate ids inside a
    single ranking are counted once, at their first (best) position.
    """
    if not rankings:
        return []
    resolved_weights = list(weights) if weights is not None else [1.0] * len(rankings)
    if len(resolved_weights) != len(rankings):
        raise ValueError("weights must have the same length as rankings")

    smoothing = max(0, int(k))
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, resolved_weights, strict=True):
        if weight <= 0:
            continue
        for rank, item_id in enumerate(_dedupe_ranking(ranking), start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (smoothing + rank)

    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return ordered[:limit] if limit is not None else ordered


def _min_max_normalize(
    hits: Sequence[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Min-max normalise scores to [0, 1], dropping non-finite values."""
    finite = [(item_id, float(score)) for item_id, score in hits if math.isfinite(score)]
    if not finite:
        return []
    low = min(score for _, score in finite)
    high = max(score for _, score in finite)
    if high == low:
        normalized = 1.0 if high != 0 else 0.0
        return [(item_id, normalized) for item_id, _ in finite]
    span = high - low
    return [(item_id, (score - low) / span) for item_id, score in finite]


def weighted_fusion(
    score_lists: Sequence[Sequence[tuple[str, float]]],
    *,
    weights: Sequence[float] | None = None,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """Min-max normalise each retriever, then combine with weights.

    Normalisation makes the two score scales comparable; without it BM25's
    unbounded scores would always drown cosine similarities.
    """
    if not score_lists:
        return []
    resolved_weights = list(weights) if weights is not None else [1.0] * len(score_lists)
    if len(resolved_weights) != len(score_lists):
        raise ValueError("weights must have the same length as score_lists")

    combined: dict[str, float] = {}
    for hits, weight in zip(score_lists, resolved_weights, strict=True):
        if weight <= 0:
            continue
        for item_id, score in _min_max_normalize(hits):
            combined[item_id] = combined.get(item_id, 0.0) + weight * score

    ordered = sorted(combined.items(), key=lambda pair: pair[1], reverse=True)
    return ordered[:limit] if limit is not None else ordered


class BM25Index:
    """An in-process BM25 index over ``CorpusDocument`` items."""

    def __init__(
        self,
        documents: Sequence[CorpusDocument],
        *,
        use_jieba: bool = True,
        stopwords: Iterable[str] | None = None,
        max_query_tokens: int | None = None,
        query_expansion: str = "none",
        feedback_docs: int = 3,
        max_feedback_terms: int = 10,
    ):
        if not is_bm25_available():
            raise ImportError(
                "rank-bm25 is not installed. Install the 'hybrid-search' extra: "
                "pip install MemoryOS[hybrid-search]"
            )
        from rank_bm25 import BM25Okapi

        self._documents = list(documents)
        self._stopwords = stopwords
        self._use_jieba = use_jieba
        self._max_query_tokens = max_query_tokens
        self._query_expansion = query_expansion
        self._feedback_docs = max(1, feedback_docs)
        self._max_feedback_terms = max(1, max_feedback_terms)
        self._tokenized: list[list[str]] = [
            self._tokenize(document.text) for document in self._documents
        ]
        self._has_content = any(self._tokenized)
        # BM25Okapi divides by the average document length; skip the model
        # entirely when every document tokenises to nothing.
        self._model = BM25Okapi(self._tokenized) if self._has_content else None

    def _tokenize(self, text: str | None, *, max_tokens: int | None = None) -> list[str]:
        return tokenize(
            text,
            use_jieba=self._use_jieba,
            stopwords=self._stopwords,
            max_tokens=max_tokens,
        )

    def search(self, query: str | None, top_k: int) -> list[tuple[str, float]]:
        """Return at most ``top_k`` ``(item_id, score)`` pairs, best first."""
        if self._model is None or top_k <= 0:
            return []
        query_tokens = list(
            dict.fromkeys(self._tokenize(query, max_tokens=self._max_query_tokens))
        )  # duplicates would double-count repeated query terms
        if not query_tokens:
            return []
        try:
            scores = self._model.get_scores(query_tokens)
        except (ZeroDivisionError, ValueError) as exc:
            logger.warning("BM25 scoring failed, skipping lexical recall: %s", exc)
            return []

        expanded = self._expand_query(query_tokens, scores)
        if expanded is not None:
            try:
                scores = self._model.get_scores(expanded)
            except (ZeroDivisionError, ValueError) as exc:
                logger.warning("BM25 RM3 rescoring failed, keeping first pass: %s", exc)

        query_set = set(query_tokens)
        match_set = set(expanded) if expanded else query_set
        ranked: list[tuple[str, float]] = []
        for document, tokens, score in zip(self._documents, self._tokenized, scores, strict=True):
            # Keep documents that genuinely share a term even when IDF is zero
            # (a term present in half of a tiny corpus scores exactly 0).
            if match_set.isdisjoint(tokens):
                continue
            numeric = float(score)
            if math.isfinite(numeric):
                ranked.append((document.item_id, numeric))
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked[:top_k]

    def _expand_query(
        self, query_tokens: list[str], scores: Sequence[float]
    ) -> list[str] | None:
        """RM3-lite: append terms from the top pseudo-relevant documents.

        Terms are appended once each and capped, so the original query keeps
        its weight while a few corpus-specific terms refine the second pass.
        Returns ``None`` when there is nothing useful to add.
        """
        if self._query_expansion != "rm3":
            return None
        order = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
        seen = set(query_tokens)
        feedback: list[str] = []
        for idx in order[: self._feedback_docs]:
            for token in self._tokenized[idx]:
                if token in seen:
                    continue
                seen.add(token)
                feedback.append(token)
                if len(feedback) >= self._max_feedback_terms:
                    return query_tokens + feedback
        return query_tokens + feedback if feedback else None


@dataclass
class HybridSearcher:
    """Stateful BM25 index + fusion policy for one memory collection.

    The index is rebuilt lazily and cached for ``index_ttl_seconds``.  Call
    :meth:`invalidate` after writes in the same process; the TTL bounds
    staleness for writes performed by other processes.
    """

    config: HybridSearchConfig
    clock: Callable[[], float] = time.monotonic
    _documents: list[CorpusDocument] | None = field(default=None, init=False, repr=False)
    _index: BM25Index | None = field(default=None, init=False, repr=False)
    _built_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _disabled_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.config.enabled:
            self._disabled_reason = "disabled by configuration"
        elif not self.config.has_signal():
            self._disabled_reason = "both fusion weights are zero"
        elif not is_bm25_available():
            self._disabled_reason = "rank-bm25 is not installed"
            logger.warning(
                "Hybrid search requested but rank-bm25 is missing; "
                "falling back to vector-only retrieval. "
                "Install with: pip install MemoryOS[hybrid-search]"
            )

    @property
    def enabled(self) -> bool:
        """Whether the searcher can actually contribute lexical results."""
        return self._disabled_reason is None

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    @property
    def candidate_top_k(self) -> int:
        return self.config.candidate_top_k

    def invalidate(self) -> None:
        """Drop the cached corpus/index; the next search rebuilds it."""
        with self._lock:
            self._documents = None
            self._index = None
            self._built_at = 0.0

    def _load_index(
        self, document_provider: Callable[[], Sequence[CorpusDocument]]
    ) -> tuple[list[CorpusDocument], BM25Index | None]:
        now = self.clock()
        with self._lock:
            fresh = (
                self._index is not None
                and self.config.index_ttl_seconds > 0
                and (now - self._built_at) <= self.config.index_ttl_seconds
            )
            if fresh:
                return self._documents or [], self._index

            documents = list(document_provider() or [])
            index = (
                BM25Index(
                    documents,
                    use_jieba=self.config.use_jieba,
                    stopwords=self.config.stopwords,
                    max_query_tokens=self.config.max_query_tokens,
                    query_expansion=self.config.query_expansion,
                    feedback_docs=self.config.rm3_feedback_docs,
                    max_feedback_terms=self.config.rm3_max_terms,
                )
                if documents
                else None
            )
            self._documents = documents
            self._index = index
            self._built_at = now
            return documents, index

    def fuse(
        self,
        query: str | None,
        vector_hits: Sequence[tuple[str, float]],
        document_provider: Callable[[], Sequence[CorpusDocument]],
        *,
        limit: int | None = None,
    ) -> list[HybridHit] | None:
        """Fuse vector hits with BM25 hits.

        Returns ``None`` when hybrid retrieval is unavailable or fails; callers
        should then keep their vector-only results.  An empty list means hybrid
        retrieval ran but found nothing (the caller should still merge the
        vector results it already has).
        """
        if not self.enabled:
            return None
        try:
            documents, index = self._load_index(document_provider)
        except Exception as exc:  # noqa: BLE001 - retrieval must degrade, never break search
            logger.warning("Failed to build BM25 index, falling back to vector-only: %s", exc)
            return None

        lexical_hits: list[tuple[str, float]] = []
        if index is not None:
            try:
                lexical_hits = index.search(query, self.config.candidate_top_k)
            except Exception as exc:  # noqa: BLE001 - lexical failure falls back to vector-only
                logger.warning("BM25 search failed, falling back to vector-only: %s", exc)
                return None

        vector_ranking = _dedupe_ranking(item_id for item_id, _ in vector_hits)
        lexical_ranking = _dedupe_ranking(item_id for item_id, _ in lexical_hits)

        if self.config.fusion == "weighted":
            fused = weighted_fusion(
                [list(vector_hits), lexical_hits],
                weights=[self.config.vector_weight, self.config.lexical_weight],
                limit=limit,
            )
        else:
            fused = reciprocal_rank_fusion(
                [vector_ranking, lexical_ranking],
                weights=[self.config.vector_weight, self.config.lexical_weight],
                k=self.config.rrf_k,
                limit=limit,
            )

        vector_positions = {item_id: rank for rank, item_id in enumerate(vector_ranking, 1)}
        lexical_positions = {item_id: rank for rank, item_id in enumerate(lexical_ranking, 1)}
        payloads = {document.item_id: document.payload for document in documents}
        return [
            HybridHit(
                item_id=item_id,
                score=score,
                vector_rank=vector_positions.get(item_id),
                lexical_rank=lexical_positions.get(item_id),
                payload=payloads.get(item_id),
            )
            for item_id, score in fused
        ]
