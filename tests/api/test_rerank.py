from types import SimpleNamespace
from unittest.mock import patch

import requests

from memos.api.handlers.cloud_handler import handle_rerank
from memos.api.product_models import RerankRequest
from memos.reranker.noop import NoopReranker


def _http_reranker(url="http://rerank/v1/rerank", model="gte-reranker"):
    return SimpleNamespace(reranker_url=url, model=model, token="", timeout=5, headers_extra=None)


def _response(payload):
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def test_rerank_returns_cloud_shape_sorted_and_truncated():
    payload = {
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.2},
            {"index": 2, "relevance_score": 0.5},
        ]
    }
    with patch("memos.api.rerank.requests.post", return_value=_response(payload)) as post:
        resp = handle_rerank(
            RerankRequest(query="q", documents=["a", "b", "c"], top_n=2), _http_reranker()
        )

    assert resp.code == 0
    assert resp.data["model"] == "gte-reranker"
    assert [(r["index"], r["document"]["text"]) for r in resp.data["results"]] == [
        (1, "b"),
        (2, "c"),
    ]
    sent = post.call_args.kwargs["json"]
    assert sent == {"model": "gte-reranker", "query": "q", "documents": ["a", "b", "c"]}


def test_rerank_surfaces_failure_instead_of_zero_scores():
    from memos.reranker.cosine_local import CosineLocalReranker

    with patch("memos.api.rerank.requests.post", side_effect=requests.ConnectionError("down")):
        failed = handle_rerank(RerankRequest(query="q", documents=["a"]), _http_reranker())
    unavailable = handle_rerank(RerankRequest(query="q", documents=["a"]), NoopReranker())
    local = handle_rerank(RerankRequest(query="q", documents=["a"]), CosineLocalReranker())

    assert failed.code == 500
    assert failed.data is None
    assert unavailable.code == 501
    assert local.code == 501
