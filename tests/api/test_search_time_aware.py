"""``time_aware`` on /product/search: the query's time wording is applied to the hits."""

import json

from types import SimpleNamespace

import pytest

from memos.api.handlers.search_handler import SearchHandler
from memos.api.product_models import APISearchRequest
from memos.search import time_understanding


class FakeCube:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.filters = []
        self.top_ks = []

    def search_memories(self, req):
        self.filters.append(req.filter)
        self.top_ks.append(req.top_k)
        return self.responses.pop(0)


class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return json.dumps(self.answer)


def _memory(memory_id, text, **meta):
    metadata = {
        "status": "activated",
        "memory_type": "LongTermMemory",
        "relativity": 0.9,
        "sources": [],
        **meta,
    }
    return {"id": memory_id, "memory": text, "metadata": metadata}


def _event(memory_id, text, day):
    return _memory(
        memory_id,
        text,
        memory_form="event",
        event_time_start=f"{day}T00:00:00+00:00",
        event_time_end=f"{day}T23:59:59+00:00",
    )


def _bucket(*memories, **other):
    return {
        "text_mem": [{"cube_id": "c", "memories": list(memories), "total_nodes": len(memories)}],
        **other,
    }


def _handler(cube, llm=None):
    handler = object.__new__(SearchHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler.deps = SimpleNamespace(reranker=None, graph_db=None, searcher=None, llm=llm)
    handler._build_cube_view = lambda req, searcher=None: cube
    return handler


def _search(handler, query, **kwargs):
    kwargs.setdefault("time_aware", True)
    req = APISearchRequest(user_id="u", readable_cube_ids=["c"], query=query, dedup="no", **kwargs)
    return handler.handle_search_memories(req).data


def _ids(data):
    return [m["id"] for m in data["text_mem"][0]["memories"]]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.delenv("SEARCH_TIME_AWARE", raising=False)
    monkeypatch.delenv("MEMOS_TIME_INTENT_LLM", raising=False)
    monkeypatch.setenv("MEMOS_QUERY_TIMEZONE", "UTC")
    time_understanding.clear_cache()


JUNE = "2025年6月的评审"


def test_time_wording_is_ignored_when_time_aware_is_off():
    cube = FakeCube(_bucket(_event("e1", "review", "2020-01-05")))

    data = _search(_handler(cube), JUNE, time_aware=False)

    assert _ids(data) == ["e1"]


def test_only_events_inside_the_window_are_kept_and_the_search_is_not_narrowed():
    cube = FakeCube(
        _bucket(_event("old", "review", "2025-01-05"), _event("june", "review", "2025-06-10"))
    )

    data = _search(_handler(cube), JUNE)

    assert _ids(data) == ["june"]
    assert cube.filters == [None]


def test_the_callers_filter_reaches_the_search_untouched():
    cube = FakeCube(_bucket(_event("june", "review", "2025-06-10")))
    caller = {"and": [{"tags": {"contains": "work"}}]}

    _search(_handler(cube), JUNE, filter=caller)

    assert cube.filters == [caller]


def test_env_switch_turns_time_awareness_on(monkeypatch):
    monkeypatch.setenv("SEARCH_TIME_AWARE", "true")
    cube = FakeCube(_bucket(_event("old", "x", "2025-01-05"), _event("june", "x", "2025-06-10")))

    data = _search(_handler(cube), JUNE, time_aware=None)

    assert _ids(data) == ["june"]


def test_request_flag_overrides_the_env_switch(monkeypatch):
    monkeypatch.setenv("SEARCH_TIME_AWARE", "true")
    cube = FakeCube(_bucket(_event("old", "x", "2025-01-05")))

    data = _search(_handler(cube), JUNE, time_aware=False)

    assert _ids(data) == ["old"]


@pytest.mark.parametrize("query", ["我喜欢什么", "我现在住哪"])
def test_a_query_without_time_cues_never_calls_the_llm(query):
    llm = FakeLLM(error=AssertionError("must not be called"))
    cube = FakeCube(_bucket(_event("old", "x", "2025-01-05")))

    data = _search(_handler(cube, llm), query)

    assert llm.calls == 0
    assert _ids(data) == ["old"]


def test_a_window_nothing_matches_returns_the_original_hits_from_one_search():
    cube = FakeCube(_bucket(_event("a", "x", "2025-01-05"), _event("b", "x", "2025-02-05")))

    data = _search(_handler(cube), JUNE)

    assert _ids(data) == ["a", "b"]
    assert len(cube.filters) == 1


def test_a_windowed_search_asks_for_more_candidates_and_returns_top_k():
    events = [_event(f"e{i}", "x", "2025-06-10") for i in range(10)]
    cube = FakeCube(_bucket(*events))

    data = _search(_handler(cube), JUNE, top_k=4)

    assert cube.top_ks == [12]
    assert _ids(data) == ["e0", "e1", "e2", "e3"]


def test_a_query_without_a_window_asks_for_exactly_top_k():
    cube = FakeCube(_bucket(_event("e", "x", "2025-06-10")))

    _search(_handler(cube), "我以前住哪", top_k=4)

    assert cube.top_ks == [4]


def test_a_state_is_read_as_it_was_in_the_window():
    shanghai = _memory(
        "home",
        "lives in Shanghai",
        valid_from="2025-09-01T00:00:00+00:00",
        history=[
            {
                "version": 1,
                "memory": "lives in Beijing",
                "update_type": "conflict",
                "created_at": "2025-01-01T00:00:00+00:00",
                "timespec": {
                    "valid_from": "2025-01-01T00:00:00+00:00",
                    "valid_to": "2025-09-01T00:00:00+00:00",
                },
            }
        ],
    )
    cube = FakeCube(_bucket(shanghai))

    hit = _search(_handler(cube), "2025年6月我住哪")["text_mem"][0]["memories"][0]

    assert hit["memory"] == "lives in Beijing"
    assert hit["metadata"]["current_memory"] == "lives in Shanghai"
    assert hit["metadata"]["matched_version"]["version"] == 1


def test_the_llm_intent_drives_the_selection_and_is_cached():
    answer = {"intent": "as_of", "start": "2025-06-01", "end": "2025-06-30", "confidence": 0.9}
    llm = FakeLLM(answer)
    query = "when I was still in Beijing"

    for _ in range(2):
        cube = FakeCube(
            _bucket(_event("old", "x", "2025-01-05"), _event("june", "x", "2025-06-10"))
        )
        data = _search(_handler(cube, llm), query)
        assert _ids(data) == ["june"]

    assert llm.calls == 1


def test_a_failing_llm_falls_back_to_the_rules():
    llm = FakeLLM(error=RuntimeError("boom"))
    cube = FakeCube(_bucket(_event("old", "x", "2025-01-05"), _event("june", "x", "2025-06-10")))

    data = _search(_handler(cube, llm), JUNE)

    assert llm.calls == 1
    assert _ids(data) == ["june"]


def test_a_current_query_drops_a_head_whose_validity_has_ended():
    ended = _memory("ended", "works at A", valid_to="2020-01-01T00:00:00+00:00")
    cube = FakeCube(_bucket(ended, _memory("live", "works at B")))

    data = _search(_handler(cube), "我现在在哪工作")

    assert _ids(data) == ["live"]


def test_other_buckets_pass_through_untouched():
    pref = {
        "cube_id": "c",
        "memories": [_memory("p1", "likes tea", memory_type="PreferenceMemory")],
        "total_nodes": 1,
    }
    cube = FakeCube(
        _bucket(
            _event("old", "x", "2025-01-05"), _event("june", "x", "2025-06-10"), pref_mem=[pref]
        )
    )

    data = _search(_handler(cube), JUNE)

    assert _ids(data) == ["june"]
    assert [m["id"] for m in data["pref_mem"][0]["memories"]] == ["p1"]
