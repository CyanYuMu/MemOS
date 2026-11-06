"""Tests for dry-run extraction, event time propagation and result sorting."""

import uuid

from memos.api.handlers.search_handler import SearchHandler
from memos.api.product_models import APIExtractRequest
from memos.memories.textual.item import (
    SourceMessage,
    TextualMemoryItem,
    TreeNodeTextualMemoryMetadata,
)
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.utils import normalize_to_utc_iso


class _FakeTextMem:
    def __init__(self):
        self.add_called = False

    def add(self, *args, **kwargs):  # pragma: no cover - must not be called
        self.add_called = True
        raise AssertionError("dry-run extraction must not persist memories")


class _FakeCube:
    def __init__(self):
        self.text_mem = _FakeTextMem()


class _FakeReader:
    save_rawfile = False

    def __init__(self, items):
        self.items = items
        self.calls = []

    def get_memory(self, *args, **kwargs):
        self.calls.append(kwargs)
        return [self.items]


def _item(chat_time: str | None = "2026-03-13 15:14:00"):
    sources = [SourceMessage(role="user", content="I like tea", chat_time=chat_time)]
    metadata = TreeNodeTextualMemoryMetadata(
        memory_type="UserMemory",
        sources=sources,
    )
    return TextualMemoryItem(id=str(uuid.uuid4()), memory="The user likes tea", metadata=metadata)


def _view(items):
    cube = _FakeCube()
    reader = _FakeReader(items)
    view = SingleCubeView(
        cube_id="cube-1",
        naive_mem_cube=cube,
        mem_reader=reader,
        mem_scheduler=None,
        logger=__import__("logging").getLogger("test"),
        searcher=None,
    )
    return view, cube, reader


def test_extract_memories_does_not_persist():
    view, cube, reader = _view([_item()])
    req = APIExtractRequest(user_id="u", messages=[{"role": "user", "content": "hi"}])

    result = view.extract_memories(req)

    assert cube.text_mem.add_called is False
    assert len(reader.calls) == 1
    assert result[0]["memory"] == "The user likes tea"
    assert result[0]["cube_id"] == "cube-1"


def test_extract_memories_propagates_chat_time_as_event_time():
    view, _, _ = _view([_item("2026-03-13 15:14:00")])
    req = APIExtractRequest(user_id="u", messages=[{"role": "user", "content": "hi"}])

    result = view.extract_memories(req)

    assert result[0]["metadata"]["chat_time"] == "2026-03-13T15:14:00+00:00"
    assert result[0]["metadata"]["created_at"].endswith("+00:00")


def test_extract_memories_keeps_unparseable_chat_time():
    view, _, _ = _view([_item("sometime last spring")])
    req = APIExtractRequest(user_id="u", messages=[{"role": "user", "content": "hi"}])

    result = view.extract_memories(req)

    assert result[0]["metadata"]["chat_time"] == "sometime last spring"


def test_normalize_to_utc_iso_handles_naive_and_offset():
    assert normalize_to_utc_iso("2026-03-13 15:14:00") == "2026-03-13T15:14:00+00:00"
    assert normalize_to_utc_iso("2026-03-13T15:14:00+08:00") == "2026-03-13T07:14:00+00:00"
    assert normalize_to_utc_iso("03:14 PM on 13 March, 2026") == "2026-03-13T15:14:00+00:00"
    assert normalize_to_utc_iso("not a date") is None


def test_sort_memories_by_chat_time():
    results = {
        "text_mem": [
            {
                "memories": [
                    {"metadata": {"chat_time": "2026-03-13T10:00:00+00:00"}},
                    {"metadata": {"chat_time": "2026-03-13T12:00:00+00:00"}},
                    {"metadata": {}},
                ]
            }
        ]
    }

    sorted_results = SearchHandler._sort_memories(results, sort_by="chat_time", sort_order="desc")

    assert sorted_results["text_mem"][0]["memories"][0]["metadata"]["chat_time"].endswith(
        "12:00:00+00:00"
    )
    assert sorted_results["text_mem"][0]["memories"][1]["metadata"]["chat_time"].endswith(
        "10:00:00+00:00"
    )
    assert sorted_results["text_mem"][0]["memories"][2]["metadata"] == {}
