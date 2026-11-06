"""Semantic selection of request ``custom_tags`` (cloud "custom tag matching").

The caller's tags are a candidate set: each memory keeps the tags the reader
generated and additionally gets the candidates whose embedding is close to the
memory's own embedding. No LLM call is added; only the already-configured
embedder is used. Off by default (``CUSTOM_TAGS_SEMANTIC_MATCH`` or the
per-request ``custom_tags_match``).
"""

from __future__ import annotations

import os
import threading
import weakref

from typing import TYPE_CHECKING, Any

import numpy as np

from memos.log import get_logger


if TYPE_CHECKING:
    from collections.abc import Sequence

    from memos.embedders.base import BaseEmbedder
    from memos.memories.textual.item import TextualMemoryItem


logger = get_logger(__name__)

MAX_CANDIDATE_TAGS = 100
_SKIPPED_MEMORY_TYPES = {"RawFileMemory", "PreferenceMemory"}
_TAG_CACHE_LIMIT = 2048

# One vector cache per embedder: vectors from different models (or dimensions) must never mix.
_tag_vectors: weakref.WeakKeyDictionary[Any, dict[str, np.ndarray]] = weakref.WeakKeyDictionary()
_tag_vectors_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        logger.warning("Invalid %s; using default %s", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        logger.warning("Invalid %s; using default %s", name, default)
        return default


def semantic_tag_match_enabled(request_flag: bool | None = None) -> bool:
    """A per-request flag wins; otherwise ``CUSTOM_TAGS_SEMANTIC_MATCH`` (default off)."""
    if request_flag is not None:
        return bool(request_flag)
    return os.getenv("CUSTOM_TAGS_SEMANTIC_MATCH", "false").strip().lower() == "true"


def _candidates(custom_tags: Sequence[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for tag in custom_tags or []:
        if isinstance(tag, str) and tag.strip():
            seen.setdefault(tag.strip(), None)
    return list(seen)[:MAX_CANDIDATE_TAGS]


def _unit(vectors: Any) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1.0, norms)


def _tag_matrix(tags: list[str], embedder: BaseEmbedder) -> np.ndarray:
    with _tag_vectors_lock:
        try:
            cache = _tag_vectors.get(embedder) or {}
        except TypeError:  # embedder cannot be weakly referenced: skip caching
            cache = {}
        vectors = {tag: cache.get(tag) for tag in tags}
    missing = [tag for tag, vector in vectors.items() if vector is None]
    if missing:
        fresh = _unit(embedder.embed(missing))
        vectors.update(zip(missing, fresh, strict=True))
        with _tag_vectors_lock:
            try:
                cache = _tag_vectors.setdefault(embedder, {})
            except TypeError:
                cache = {}
            if len(cache) + len(missing) > _TAG_CACHE_LIMIT:
                cache.clear()
            cache.update(zip(missing, fresh, strict=True))
    return np.stack([vectors[tag] for tag in tags])


def apply_semantic_custom_tags(
    items: Sequence[TextualMemoryItem],
    custom_tags: Sequence[str] | None,
    embedder: BaseEmbedder | None,
    *,
    enabled: bool | None = None,
    threshold: float | None = None,
    top_k: int | None = None,
) -> int:
    """Merge the closest candidate tags into each item's ``tags``; returns items changed.

    Never raises: a failure leaves the reader's own tags untouched.
    """
    if not semantic_tag_match_enabled(enabled) or embedder is None:
        return 0
    tags = _candidates(custom_tags)
    targets = [
        item
        for item in items
        if (item.memory or "").strip() and item.metadata.memory_type not in _SKIPPED_MEMORY_TYPES
    ]
    if not tags or not targets:
        return 0

    threshold = (
        threshold if threshold is not None else _env_float("CUSTOM_TAGS_MATCH_THRESHOLD", 0.4)
    )
    top_k = top_k if top_k is not None else _env_int("CUSTOM_TAGS_MATCH_TOP_K", 3)

    try:
        tag_matrix = _tag_matrix(tags, embedder)
        stored = [getattr(item.metadata, "embedding", None) for item in targets]
        missing = [i for i, vector in enumerate(stored) if not vector]
        if missing:
            fresh = embedder.embed([targets[i].memory for i in missing])
            for i, vector in zip(missing, fresh, strict=True):
                stored[i] = vector
        scores = _unit(stored) @ tag_matrix.T
    except Exception:
        logger.warning("Semantic custom tag matching failed; keeping reader tags", exc_info=True)
        return 0

    changed = 0
    for item, row in zip(targets, scores, strict=True):
        existing = list(item.metadata.tags or [])
        picked = [
            tags[idx]
            for idx in np.argsort(-row)[:top_k]
            if row[idx] >= threshold and tags[idx] not in existing
        ]
        if picked:
            item.metadata.tags = existing + picked
            changed += 1
    return changed
