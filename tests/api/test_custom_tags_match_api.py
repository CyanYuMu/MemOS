"""``custom_tags_match`` plumbing: request flag, sync extraction, async hand-off."""

from types import SimpleNamespace

import pytest

from memos.api.handlers.add_handler import AddHandler  # noqa: F401  (import order)
from memos.api.product_models import APIADDRequest
from memos.mem_reader import tag_matcher
from memos.mem_scheduler.task_schedule_modules.handlers import mem_read_handler
from memos.mem_scheduler.task_schedule_modules.handlers.mem_read_handler import (
    MemReadMessageHandler,
)
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.types import UserContext


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tag_matcher._tag_vectors.clear()
    for name in (
        "CUSTOM_TAGS_SEMANTIC_MATCH",
        "CUSTOM_TAGS_MATCH_THRESHOLD",
        "CUSTOM_TAGS_MATCH_TOP_K",
    ):
        monkeypatch.delenv(name, raising=False)


MESSAGES = [{"role": "user", "content": "I booked a flight to Tokyo"}]
VECTORS = {"travel": [1.0, 0.0], "food": [0.0, 1.0], "a Tokyo flight": [0.95, 0.05]}


class FakeEmbedder:
    def embed(self, texts):
        return [VECTORS[text] for text in texts]


def _memory(text="a Tokyo flight", tags=None):
    return SimpleNamespace(
        id="m1",
        memory=text,
        metadata=SimpleNamespace(
            tags=tags, memory_type="LongTermMemory", embedding=None, info={}, status="activated"
        ),
    )


def _req(**kwargs):
    return APIADDRequest(user_id="u", writable_cube_ids=["c"], messages=MESSAGES, **kwargs)


def test_request_flag_defaults_to_none_and_accepts_booleans():
    assert _req().custom_tags_match is None
    assert _req(custom_tags_match=True).custom_tags_match is True
    assert _req(custom_tags_match=False).custom_tags_match is False


# ---- sync / fast extraction ---------------------------------------------------------------


def _cube(memories):
    cube = object.__new__(SingleCubeView)
    cube.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    cube.cube_id = "c"
    cube.mem_reader = SimpleNamespace(
        embedder=FakeEmbedder(), get_memory=lambda *a, **k: [memories]
    )
    cube._resolve_extract_mode = lambda *a, **k: "fast"
    cube._apply_chat_time = lambda items: None
    return cube


def _extract(cube, req):
    return cube._extract_text_mem(req, UserContext(mem_cube_id="c", user_id="u"), "sync")[0]


def test_extraction_adds_matching_candidates_when_requested():
    memory = _memory(tags=["mode:fast"])

    _extract(_cube([memory]), _req(custom_tags=["travel", "food"], custom_tags_match=True))

    assert memory.metadata.tags == ["mode:fast", "travel"]


def test_extraction_leaves_tags_alone_by_default():
    memory = _memory(tags=["mode:fast"])

    _extract(_cube([memory]), _req(custom_tags=["travel", "food"]))

    assert memory.metadata.tags == ["mode:fast"]


def test_extraction_without_candidates_is_unchanged():
    memory = _memory(tags=["mode:fast"])

    _extract(_cube([memory]), _req(custom_tags_match=True))

    assert memory.metadata.tags == ["mode:fast"]


# ---- async hand-off -----------------------------------------------------------------------


def test_async_message_carries_candidates_and_flag():
    submitted = []
    cube = object.__new__(SingleCubeView)
    cube.logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)
    cube.cube_id = "c"
    cube.naive_mem_cube = SimpleNamespace()
    cube.mem_scheduler = SimpleNamespace(
        submit_messages=lambda messages: submitted.extend(messages)
    )

    cube._schedule_memory_tasks(
        _req(custom_tags=["travel"], custom_tags_match=True), UserContext(), ["m1"], "async"
    )

    info = submitted[0].info
    assert info["custom_tags"] == ["travel"]
    assert info["custom_tags_match"] is True


class _Reader:
    save_rawfile = False
    graph_db = None
    memory_version_switch = "on"
    embedder = FakeEmbedder()

    def __init__(self):
        self.custom_tags = "unset"

    def fine_transfer_simple_mem(self, items, **kwargs):
        self.custom_tags = kwargs.get("custom_tags")
        return [[_memory()]]


def _run_scheduler(monkeypatch, info):
    added = []
    reader = _Reader()
    handler = object.__new__(MemReadMessageHandler)
    handler.scheduler_context = SimpleNamespace(get_mem_reader=lambda: reader)
    handler._try_submit_organize_task = lambda **kwargs: None
    monkeypatch.setattr(mem_read_handler, "_archive_two_stage_coarse_nodes", lambda **kw: None)
    monkeypatch.setattr(mem_read_handler, "is_playground_api", lambda: True)
    text_mem = SimpleNamespace(
        get=lambda mem_id, user_name=None: _memory(),
        add=lambda items, user_name=None: added.append(list(items)) or ["n1"],
    )
    handler._process_memories_with_reader(
        mem_ids=["m1"],
        user_id="u",
        mem_cube_id="c",
        text_mem=text_mem,
        user_name="c",
        custom_tags=info.get("custom_tags"),
        task_id="t",
        info=dict(info),
    )
    return reader, added


@pytest.mark.parametrize("flag,expected", [(True, ["travel"]), (None, None), (False, None)])
def test_scheduler_applies_matching_to_the_fine_result(monkeypatch, flag, expected):
    reader, added = _run_scheduler(
        monkeypatch, {"custom_tags": ["travel", "food"], "custom_tags_match": flag}
    )

    assert reader.custom_tags == ["travel", "food"]
    assert added[0][0].metadata.tags == expected
