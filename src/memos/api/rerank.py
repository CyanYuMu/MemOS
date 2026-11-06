"""``/product/rerank``: score arbitrary documents against a query (cloud ``rerank``).

The search path reranks ``TextualMemoryItem`` objects and degrades to zero scores on
failure. A public route cannot do that: a caller would treat the zeros as real
relevance, so the failure has to surface. HTTP rerankers are therefore called
directly here and errors propagate.
"""

from __future__ import annotations

from typing import Any

import requests

from memos.log import get_logger
from memos.reranker.noop import NoopReranker


logger = get_logger(__name__)


class RerankUnavailableError(RuntimeError):
    """No reranker is configured for this deployment."""


def _http_scores(reranker: Any, query: str, documents: list[str]) -> list[tuple[int, float]]:
    headers = {
        "Content-Type": "application/json",
        **(getattr(reranker, "headers_extra", None) or {}),
    }
    if getattr(reranker, "token", ""):
        headers["Authorization"] = f"Bearer {reranker.token}"
    payload = {"model": getattr(reranker, "model", ""), "query": query, "documents": documents}
    response = requests.post(
        reranker.reranker_url,
        headers=headers,
        json=payload,
        timeout=getattr(reranker, "timeout", 60),
    )
    response.raise_for_status()
    data = response.json()

    if "results" in data:
        return [
            (int(row["index"]), float(row.get("relevance_score", row.get("score", 0.0))))
            for row in data["results"]
            if isinstance(row.get("index"), int) and 0 <= row["index"] < len(documents)
        ]
    if "data" in data:
        return [
            (index, float(row.get("score", 0.0)))
            for index, row in enumerate(data["data"][: len(documents)])
        ]
    raise ValueError("reranker returned an unexpected response shape")


def _local_scores(reranker: Any, query: str, documents: list[str]) -> list[tuple[int, float]]:
    # A local reranker scores node embeddings and these documents have none, so it
    # would answer with a constant score. Refuse instead of returning noise.
    raise RerankUnavailableError(
        f"reranker backend {type(reranker).__name__} cannot score raw text; "
        "configure an HTTP reranker (MOS_RERANKER_URL)"
    )


def score_documents(reranker: Any, query: str, documents: list[str]) -> list[tuple[int, float]]:
    """Return ``(document index, score)`` pairs; raises instead of inventing scores."""
    if reranker is None or isinstance(reranker, NoopReranker):
        raise RerankUnavailableError("no reranker is configured")
    if getattr(reranker, "reranker_url", None):
        return _http_scores(reranker, query, documents)
    return _local_scores(reranker, query, documents)


def rerank_model_name(reranker: Any) -> str:
    return getattr(reranker, "model", None) or getattr(reranker, "reranker_url", None) or "local"
