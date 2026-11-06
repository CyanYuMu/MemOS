"""``time_aware`` and ``include_history`` on /product/search."""

import json

from types import SimpleNamespace

import pytest

from memos.api.handlers.search_handler import SearchHandler, _history_versions
from memos.api.product_models import APISearchRequest


def _memory(memory_id, text, history=None, status="activated"):
    metadata = {
        "status": status,
        "memory_type": "LongTermMemory",
        "relativity": 0.9,
        "sources": [],
    }
    if history is not None:
        metadata["history"] = history
    return {"id": memory_id, "memory": text, "metadata": metadata}


def _bucket(*memories):
    return {
        "text_mem": [{"cube_id": "c", "memories": list(memories), "total_nodes": len(memories)}]
    }


class FakeCube:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.filters = []

    def search_memories(self, req):
        self.filters.append(req.filter)
        return self.responses.pop(0)


def _handler(cube):
    handler = object.__new__(SearchHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler.deps = SimpleNamespace(reranker=None, graph_db=None, searcher=None)
    handler._build_cube_view = lambda req, searcher=None: cube
    return handler


def _search(handler, query, **kwargs):
    req = APISearchRequest(user_id="u", readable_cube_ids=["c"], query=query, dedup="no", **kwargs)
    return handler.handle_search_memories(req).data


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.delenv("SEARCH_TIME_AWARE", raising=False)
    monkeypatch.delenv("SEARCH_HISTORY_MAX_VERSIONS", raising=False)


# ---- include_history --------------------------------------------------------------------

HISTORY = [
    {
        "version": 1,
        "memory": "lives in Beijing",
        "update_type": "update",
        "created_at": "2025-01-01",
    },
    {
        "version": 2,
        "memory": "lives in Shenzhen",
        "update_type": "update",
        "created_at": "2025-06-01",
        "timespec": {"valid_from": "2025-06-01T00:00:00+00:00"},
    },
]


def test_history_is_not_attached_by_default():
    cube = FakeCube(_bucket(_memory("m1", "lives in Shanghai", history=HISTORY)))

    data = _search(_handler(cube), "where do I live")

    assert "history_versions" not in data["text_mem"][0]["memories"][0]["metadata"]


def test_include_history_attaches_versions_newest_first():
    cube = FakeCube(_bucket(_memory("m1", "lives in Shanghai", history=HISTORY)))

    data = _search(_handler(cube), "where do I live", include_history=True)

    versions = data["text_mem"][0]["memories"][0]["metadata"]["history_versions"]
    assert [v["memory"] for v in versions] == ["lives in Shenzhen", "lives in Beijing"]
    assert versions[0] == {
        "version": 2,
        "memory": "lives in Shenzhen",
        "update_type": "update",
        "created_at": "2025-06-01",
        "timespec": {"valid_from": "2025-06-01T00:00:00+00:00"},
    }
    assert "timespec" not in versions[1]


def test_include_history_does_not_change_which_memories_are_returned():
    with_history = FakeCube(
        _bucket(
            _memory("m1", "a", history=HISTORY),
            _memory("m2", "b"),
            _memory("m3", "c", status="archived"),
        )
    )
    without = FakeCube(
        _bucket(
            _memory("m1", "a", history=HISTORY),
            _memory("m2", "b"),
            _memory("m3", "c", status="archived"),
        )
    )

    a = _search(_handler(with_history), "q", include_history=True)
    b = _search(_handler(without), "q")

    def ids(data):
        return [m["id"] for m in data["text_mem"][0]["memories"]]

    assert ids(a) == ids(b) == ["m1", "m2"]


def test_memories_without_history_get_no_key():
    cube = FakeCube(_bucket(_memory("m1", "a"), _memory("m2", "b", history=[])))

    data = _search(_handler(cube), "q", include_history=True)

    for memory in data["text_mem"][0]["memories"]:
        assert "history_versions" not in memory["metadata"]


def test_history_versions_are_capped(monkeypatch):
    monkeypatch.setenv("SEARCH_HISTORY_MAX_VERSIONS", "1")
    cube = FakeCube(_bucket(_memory("m1", "now", history=HISTORY)))

    data = _search(_handler(cube), "q", include_history=True)

    versions = data["text_mem"][0]["memories"][0]["metadata"]["history_versions"]
    assert [v["version"] for v in versions] == [2]


def test_past_intent_attaches_history_when_time_aware():
    cube = FakeCube(_bucket(_memory("m1", "lives in Shanghai", history=HISTORY)))

    data = _search(_handler(cube), "我以前住哪", time_aware=True)

    assert cube.filters == [None]
    assert "history_versions" in data["text_mem"][0]["memories"][0]["metadata"]


def test_past_intent_is_ignored_when_time_aware_is_off():
    cube = FakeCube(_bucket(_memory("m1", "lives in Shanghai", history=HISTORY)))

    data = _search(_handler(cube), "我以前住哪")

    assert "history_versions" not in data["text_mem"][0]["memories"][0]["metadata"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        [],
        [{"memory": ""}, {"memory": None}, 3, "oops"],
        {"memory": "not a list"},
    ],
)
def test_unusable_history_yields_nothing(raw):
    assert _history_versions(raw) == []


def test_history_stored_as_json_strings_is_parsed():
    raw = [json.dumps({"version": 3, "memory": "old", "created_at": "2025-02-02"})]

    assert _history_versions(raw)[0]["memory"] == "old"
    assert _history_versions(json.dumps(raw))[0]["version"] == 3


def test_versions_without_numbers_sort_by_creation_time():
    raw = [
        {"memory": "older", "created_at": "2025-01-01"},
        {"memory": "newer", "created_at": "2025-03-01"},
    ]

    assert [v["memory"] for v in _history_versions(raw)] == ["newer", "older"]
