import copy

from types import SimpleNamespace

import pytest

from fastapi import HTTPException

from memos.api.handlers.search_handler import SearchHandler
from memos.api.product_models import APISearchRequest
from memos.plugins import hooks
from memos.plugins.hook_defs import H
from memos.search.context_format import (
    apply_context_format,
    choose_mode,
    normalize_mode,
    render_sources,
)


CHAT = [
    {"type": "chat", "role": "user", "chat_time": "2025-06-01", "content": "I flew to Hangzhou"},
    {"type": "chat", "role": "assistant", "content": "Nice trip"},
    {"type": "chat", "role": "user", "chat_time": "2025-06-01", "content": "I flew to Hangzhou"},
]


def _hit(memory_id, text, memory_type="LongTermMemory", sources=None, **meta):
    metadata = {
        "status": "activated",
        "memory_type": memory_type,
        "memory": text,
        "sources": sources or [],
        **meta,
    }
    return {"id": memory_id, "memory": text, "metadata": metadata}


def _results(*hits, **other):
    return {
        "text_mem": [{"cube_id": "c", "memories": list(hits), "total_nodes": len(hits)}],
        **other,
    }


def test_memory_and_the_unset_format_leave_the_results_untouched():
    results = _results(_hit("a", "went to Hangzhou", sources=CHAT))

    assert apply_context_format(results, "q", "memory") is results


def test_sources_replace_the_text_of_conversation_hits_and_keep_the_extracted_text():
    plain = _hit("plain", "no sources")
    pref = _hit("pref", "likes tea", memory_type="PreferenceMemory", sources=CHAT)
    past = _hit("past", "old", sources=CHAT, matched_version={"version": 1})
    results = _results(_hit("a", "went to Hangzhou", sources=CHAT), plain, pref, past, pref_mem=[1])
    snapshot = copy.deepcopy(results)

    out = apply_context_format(results, "q", "sources")

    assert results == snapshot
    first, *rest = out["text_mem"][0]["memories"]
    assert first["memory"] == "[2025-06-01] user: I flew to Hangzhou\nassistant: Nice trip"
    assert first["metadata"]["memory"] == first["memory"]
    assert first["metadata"]["extracted_memory"] == "went to Hangzhou"
    assert first["metadata"]["context_format"] == "sources"
    assert rest == [plain, pref, past] and out["pref_mem"] == [1]


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ("他原话是怎么说的", "sources"),
        ("我什么时候去的杭州", "sources"),
        ("I went there how many times", "sources"),
        ("哪件事最先发生", "sources"),
        ("我喜欢什么", "memory"),
    ],
)
def test_mixed_reads_the_source_only_when_the_query_asks_for_detail(query, mode):
    assert choose_mode(query, "mixed") == mode


def test_the_names_are_case_insensitive_and_heuristic_routing_means_mixed():
    assert normalize_mode(" Sources ") == "sources"
    assert normalize_mode("heuristic_routing") == "mixed"
    assert normalize_mode("") == "memory"
    assert normalize_mode("plugin-owned-format") is None


def test_rendering_is_cut_at_a_line_boundary_and_a_single_long_line_is_cut_hard():
    sources = [{"role": "user", "content": "a" * 30}, {"role": "user", "content": "b" * 30}]

    assert render_sources(sources, 50) == "user: " + "a" * 30
    assert render_sources(sources, 10) == "user: aaa…"


def _handler_request(context_format):
    return APISearchRequest(user_id="u", query="q", context_format=context_format)


def test_an_unknown_format_is_rejected_unless_a_render_plugin_owns_it(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        SearchHandler._context_mode(_handler_request("plugin-owned-format"))
    assert caught.value.status_code == 422 and "heuristic_routing" in caught.value.detail

    monkeypatch.setitem(hooks._hooks, H.SEARCH_CONTEXT_RENDER, [lambda **kwargs: None])

    assert SearchHandler._context_mode(_handler_request("plugin-owned-format")) == "memory"
    assert SearchHandler._context_mode(_handler_request("sources")) == "sources"


class _Cube:
    def __init__(self, results):
        self.results = results
        self.searches = 0

    def search_memories(self, req):
        self.searches += 1
        return self.results


def _search(cube, **kwargs):
    handler = object.__new__(SearchHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler.deps = SimpleNamespace(reranker=None, graph_db=None, searcher=None, llm=None)
    handler._build_cube_view = lambda req, searcher=None: cube
    req = APISearchRequest(user_id="u", readable_cube_ids=["c"], dedup="no", **kwargs)
    return handler.handle_search_memories(req)


def test_the_search_returns_the_sources_and_a_rejected_format_never_searches():
    cube = _Cube(_results(_hit("a", "went to Hangzhou", sources=CHAT)))

    hit = _search(cube, query="我什么时候去的杭州", context_format="mixed").data["text_mem"][0][
        "memories"
    ][0]
    assert hit["memory"].startswith("[2025-06-01] user: I flew")

    cube.searches = 0
    with pytest.raises(HTTPException):
        _search(cube, query="q", context_format="nope")
    assert cube.searches == 0
