"""Configuration for hybrid (vector + lexical) retrieval."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from memos.configs.base import BaseConfig


class HybridSearchConfig(BaseConfig):
    """Configuration for the optional BM25 + vector hybrid retriever.

    The retriever is opt-in: leave ``GeneralTextMemoryConfig.hybrid_search`` as
    ``None`` and behaviour is byte-for-byte the previous vector-only path.

    It requires a vector backend that can snapshot item text without vectors
    (``BaseVecDB.supports_text_snapshot``; currently ``qdrant``). Requesting it
    on any other backend raises ``ConfigurationError`` at memory construction
    instead of silently degrading to vector-only search.
    """

    enabled: bool = Field(
        True,
        description="Master switch. When False the memory falls back to vector-only retrieval.",
    )
    fusion: Literal["rrf", "weighted"] = Field(
        "rrf",
        description=(
            "'rrf' uses Reciprocal Rank Fusion (scale-free, robust to incomparable "
            "score ranges). 'weighted' min-max normalises each retriever's scores "
            "before applying the configured weights."
        ),
    )
    candidate_top_k: int = Field(
        50,
        ge=1,
        description=(
            "How many candidates each retriever fetches before fusion. Must be at "
            "least the requested top_k for hybrid retrieval to add value."
        ),
    )
    rrf_k: int = Field(
        60,
        ge=0,
        description="RRF smoothing constant; 60 is the value from the original paper.",
    )
    vector_weight: float = Field(
        1.0,
        ge=0.0,
        description="Weight applied to the vector ranking during fusion.",
    )
    lexical_weight: float = Field(
        1.0,
        ge=0.0,
        description="Weight applied to the BM25 ranking during fusion.",
    )
    use_jieba: bool = Field(
        True,
        description="Use jieba for Chinese segmentation when installed; n-gram fallback otherwise.",
    )
    query_expansion: Literal["none", "rm3"] = Field(
        "none",
        description=(
            "Optional lexical query expansion. 'rm3' runs a second BM25 pass with "
            "terms mined from the top pseudo-relevant documents. Off by default: "
            "it helps short/ambiguous queries but can dilute precise ones."
        ),
    )
    rm3_feedback_docs: int = Field(
        3,
        ge=1,
        le=50,
        description="How many top documents RM3 mines feedback terms from.",
    )
    rm3_max_terms: int = Field(
        10,
        ge=1,
        le=100,
        description="Cap on feedback terms added to the second-pass query.",
    )
    stopwords: list[str] | None = Field(
        None,
        description="Extra stopwords merged with the built-in Chinese/English lists.",
    )
    index_ttl_seconds: float = Field(
        60.0,
        ge=0.0,
        description=(
            "Maximum age of the in-process BM25 index. A positive TTL bounds "
            "staleness when another process writes to the same collection; 0 "
            "rebuilds on every search. Rebuilding snapshots the whole collection "
            "payload-only (no embeddings), so cost grows with memory count."
        ),
    )
    max_query_tokens: int = Field(
        256,
        ge=1,
        description="Hard cap on query tokens to keep pathological inputs bounded.",
    )

    @field_validator("vector_weight", "lexical_weight")
    @classmethod
    def validate_weights(cls, value: float) -> float:
        if value < 0:
            raise ValueError("fusion weights must be non-negative")
        return value

    def has_signal(self) -> bool:
        """Whether at least one retriever contributes to the fused score."""
        return self.vector_weight > 0 or self.lexical_weight > 0
