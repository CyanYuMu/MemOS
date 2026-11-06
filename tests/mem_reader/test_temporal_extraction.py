"""Extraction of ``memory_form`` and the absolute ``event_time``."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from memos.mem_reader import temporal
from memos.mem_reader.multi_modal_struct import MultiModalStructMemReader
from memos.mem_reader.temporal import append_temporal_section, temporal_fields
from memos.memories.textual.item import (
    SourceMessage,
    TextualMemoryItem,
    TreeNodeTextualMemoryMetadata,
)


# ───────────────────────────── temporal_fields ─────────────────────────────


def test_a_state_carries_only_its_form():
    assert temporal_fields({"memory_form": "state"}) == {"memory_form": "state"}


def test_anything_missing_or_unknown_means_state_without_fields():
    assert temporal_fields({}) == {}
    assert temporal_fields({"memory_form": "maybe"}) == {}
    assert temporal_fields({"memory_form": None}) == {}


@pytest.fixture(autouse=True)
def _default_timezone(monkeypatch):
    monkeypatch.delenv("MEMOS_QUERY_TIMEZONE", raising=False)


def test_an_event_date_range_covers_whole_local_days():

    fields = temporal_fields(
        {"memory_form": "event", "event_time": {"start": "2025-03-10", "end": "2025-03-12"}}
    )

    assert fields["memory_form"] == "event"
    assert fields["event_time_start"] == "2025-03-09T16:00:00+00:00"
    assert fields["event_time_end"] == "2025-03-12T15:59:59+00:00"


def test_a_single_date_event_spans_that_day():
    fields = temporal_fields({"memory_form": "event", "event_time": "2025-03-10"})

    assert fields["event_time_start"] == "2025-03-09T16:00:00+00:00"
    assert fields["event_time_end"] == "2025-03-10T15:59:59+00:00"


def test_a_datetime_with_an_offset_is_converted_to_utc():
    fields = temporal_fields(
        {"memory_form": "event", "event_time": {"start": "2025-03-10T09:30:00+08:00"}}
    )

    assert fields["event_time_start"] == "2025-03-10T01:30:00+00:00"


def test_a_naive_datetime_is_read_as_local_wall_clock():
    fields = temporal_fields(
        {"memory_form": "event", "event_time": {"start": "2025-03-10T09:30:00"}}
    )

    assert fields["event_time_start"] == "2025-03-10T01:30:00+00:00"


def test_a_space_separated_local_datetime_is_also_read_as_local():
    fields = temporal_fields({"memory_form": "event", "event_time": {"start": "2025-03-10 09:30"}})

    assert fields["event_time_start"] == "2025-03-10T01:30:00+00:00"


def test_a_trailing_z_means_utc():
    fields = temporal_fields(
        {"memory_form": "event", "event_time": {"start": "2025-03-10T09:30:00Z"}}
    )

    assert fields["event_time_start"] == "2025-03-10T09:30:00+00:00"


def test_an_event_without_a_usable_time_keeps_the_form_only():
    assert temporal_fields({"memory_form": "event"}) == {"memory_form": "event"}
    assert temporal_fields({"memory_form": "event", "event_time": {"start": "sometime"}}) == {
        "memory_form": "event"
    }
    assert temporal_fields({"memory_form": "event", "event_time": 42}) == {"memory_form": "event"}


def test_a_reversed_range_is_put_in_order():
    fields = temporal_fields(
        {"memory_form": "event", "event_time": {"start": "2025-05-02", "end": "2025-05-01"}}
    )

    assert fields["event_time_start"] < fields["event_time_end"]


def test_a_state_never_gets_an_event_time():
    fields = temporal_fields({"memory_form": "state", "event_time": {"start": "2025-03-10"}})

    assert fields == {"memory_form": "state"}


# ───────────────────────────── prompt section ─────────────────────────────


def test_the_section_follows_the_prompt_language():
    assert "Temporal fields" in append_temporal_section("Return JSON. Conversation: hello")
    assert "时间字段" in append_temporal_section("请返回 JSON。对话：你好")


def test_the_section_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MEMOS_EXTRACT_TEMPORAL", "false")

    assert append_temporal_section("prompt") == "prompt"
    assert temporal.extraction_enabled() is False


class RecordingLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, messages):
        self.prompts.append(messages[0]["content"])
        return '{"memory list": [], "summary": ""}'


def _reader():
    reader = object.__new__(MultiModalStructMemReader)
    reader.llm = RecordingLLM()
    reader.config = SimpleNamespace(remove_prompt_example=False)
    return reader


def test_chat_extraction_asks_for_the_temporal_fields():
    reader = _reader()

    reader._get_llm_response("user: 昨天我去西安出差了", None)

    assert "memory_form" in reader.llm.prompts[0]
    assert "event_time" in reader.llm.prompts[0]


def test_document_extraction_is_left_alone():
    reader = _reader()

    reader._get_llm_response("a long document", None, prompt_type="doc")

    assert "memory_form" not in reader.llm.prompts[0]


# ───────────────────────────── end to end through the reader ─────────────────────────────


def _fine_reader(response):
    reader = object.__new__(MultiModalStructMemReader)
    reader.memory_version_switch = "off"
    reader.save_rawfile = False
    reader.embedder = MagicMock()
    reader.embedder.embed.return_value = [[0.1, 0.2]]
    reader._get_llm_response = MagicMock(return_value=response)
    return reader


def _fast_item():
    return TextualMemoryItem(
        memory="user: 昨天我去西安出差了",
        metadata=TreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory",
            sources=[SourceMessage(type="chat", role="user", content="昨天我去西安出差了")],
        ),
    )


def test_extracted_event_fields_land_on_the_memory_node():
    reader = _fine_reader(
        {
            "memory list": [
                {
                    "key": "西安出差",
                    "memory_type": "LongTermMemory",
                    "value": "用户 2025年3月10日去西安出差。",
                    "tags": ["出差"],
                    "memory_form": "event",
                    "event_time": {"start": "2025-03-10", "end": "2025-03-10"},
                },
                {
                    "key": "住址",
                    "memory_type": "UserMemory",
                    "value": "用户住在上海。",
                    "tags": ["住址"],
                },
            ],
            "summary": "s",
        }
    )

    items = reader._process_string_fine([_fast_item()], {"user_id": "u", "session_id": "s"})

    by_key = {i.metadata.key: i.metadata for i in items}
    assert by_key["西安出差"].memory_form == "event"
    assert by_key["西安出差"].event_time_start == "2025-03-09T16:00:00+00:00"
    assert by_key["住址"].memory_form is None, "a missing form stays unset and reads as a state"


@pytest.mark.parametrize("bad", [{"memory_form": 7}, {"memory_form": "event", "event_time": "x"}])
def test_garbage_from_the_llm_never_breaks_extraction(bad):
    reader = _fine_reader(
        {
            "memory list": [
                {"key": "k", "memory_type": "LongTermMemory", "value": "v", "tags": [], **bad}
            ],
            "summary": "s",
        }
    )

    items = reader._process_string_fine([_fast_item()], {"user_id": "u", "session_id": "s"})

    assert len(items) == 1
