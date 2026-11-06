"""T2.6 event memory: extraction, chat_time priority, recall bucket."""

import json

from typing import ClassVar

from memos.api.handlers.formatters_handler import post_process_textual_mem
from memos.mem_reader.event_extractor import (
    EVENT_EXTRACTION_PROMPT,
    MAX_WINDOW_CHARS,
    EventExtractor,
)


class FakeLLM:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    def generate(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


MESSAGES = [
    {
        "role": "user",
        "content": "明天下午三点我在北京和客户开会",
        "role_name": "张三",
        "chat_time": "2026-10-20T09:00:00+08:00",
    },
    {
        "role": "assistant",
        "content": "好的",
        "role_name": "助手",
        "chat_time": "2026-10-20T09:01:00+08:00",
    },
]

EVENT_JSON = """[
  {
    "title": "与客户开会",
    "content": "张三明天下午三点在北京和客户开会",
    "event_time": ["明天下午三点"],
    "event_location": ["北京"],
    "event_roles": ["张三", "客户"]
  }
]"""


def test_extracts_event_with_structured_fields():
    extractor = EventExtractor(FakeLLM(EVENT_JSON))

    items = extractor.extract(MESSAGES, info={"user_id": "u1"}, session_id="s1")

    assert len(items) == 1
    item = items[0]
    assert item.metadata.memory_type == "EventMemory"
    assert item.metadata.status == "activated"
    assert item.metadata.key == "与客户开会"
    assert item.memory == "与客户开会: 张三明天下午三点在北京和客户开会"
    assert item.metadata.event_time == ["明天下午三点"]
    assert item.metadata.event_location == ["北京"]
    assert item.metadata.event_roles == ["张三", "客户"]
    assert item.metadata.session_id == "s1"


def test_chat_time_takes_priority_over_model_guess():
    extractor = EventExtractor(FakeLLM(EVENT_JSON))

    item = extractor.extract(MESSAGES)[0]

    # First source message's chat_time wins, normalized to UTC.
    assert item.metadata.chat_time == "2026-10-20T01:00:00+00:00"
    assert item.metadata.sources[0].chat_time == item.metadata.chat_time


def test_conversation_prompt_carries_speaker_names():
    llm = FakeLLM(EVENT_JSON)

    EventExtractor(llm).extract(MESSAGES)

    assert "张三 (user): 明天下午三点我在北京和客户开会" in llm.prompts[0]
    assert "助手 (assistant): 好的" in llm.prompts[0]


def test_markdown_fenced_json_is_accepted():
    extractor = EventExtractor(FakeLLM(f"```json\n{EVENT_JSON}\n```"))

    assert len(extractor.extract(MESSAGES)) == 1


def test_invalid_json_yields_no_events():
    assert EventExtractor(FakeLLM("I cannot help with that")).extract(MESSAGES) == []
    assert EventExtractor(FakeLLM("{not json")).extract(MESSAGES) == []


def test_llm_failure_is_swallowed():
    extractor = EventExtractor(FakeLLM(RuntimeError("llm down")))

    assert extractor.extract(MESSAGES) == []


def test_empty_content_events_are_dropped():
    output = '[{"title": "空", "content": "  "}, {"title": "ok", "content": "real"}]'

    items = EventExtractor(FakeLLM(output)).extract(MESSAGES)

    assert [item.metadata.key for item in items] == ["ok"]


def test_no_messages_or_no_llm_short_circuits():
    assert EventExtractor(FakeLLM(EVENT_JSON)).extract([]) == []
    assert EventExtractor(None).extract(MESSAGES) == []


def test_formatter_puts_events_into_their_own_bucket():
    results = {"text_mem": [], "tool_mem": [], "pref_mem": [], "dream_mem": []}
    formatted = [
        {
            "id": "e1",
            "memory": "event",
            "metadata": {"memory_type": "EventMemory"},
        },
        {
            "id": "f1",
            "memory": "fact",
            "metadata": {"memory_type": "LongTermMemory"},
        },
    ]

    out = post_process_textual_mem(results, formatted, "cube-1")

    assert [mem["id"] for mem in out["event_mem"][0]["memories"]] == ["e1"]
    assert [mem["id"] for mem in out["text_mem"][0]["memories"]] == ["f1"]


def test_sort_and_trim_keeps_event_memories():
    from memos.memories.textual.item import (
        SearchedTreeNodeTextualMemoryMetadata,
        TextualMemoryItem,
    )
    from memos.memories.textual.tree_text_memory.retrieve.searcher import Searcher

    searcher = object.__new__(Searcher)
    event = TextualMemoryItem(
        id="11111111-1111-4111-8111-111111111111",
        memory="Alice went to Beijing",
        metadata=SearchedTreeNodeTextualMemoryMetadata(
            memory_type="EventMemory", status="activated"
        ),
    )
    fact = TextualMemoryItem(
        id="22222222-2222-4222-8222-222222222222",
        memory="Alice likes tea",
        metadata=SearchedTreeNodeTextualMemoryMetadata(
            memory_type="LongTermMemory", status="activated"
        ),
    )

    results = searcher._sort_and_trim([(event, 0.9), (fact, 0.8)], top_k=10)

    assert {item.id for item in results} == {
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    }


class WindowLLM(FakeLLM):
    """Answers with every event whose marker appears in the prompt it was given."""

    EVENTS: ClassVar[dict[str, dict]] = {
        "北京": {"title": "北京出差", "content": "去北京出差", "event_time": ["一月"]},
        "周会": {"title": "周会", "content": "开了周会", "event_time": ["周一"]},
        "杭州": {"title": "杭州旅游", "content": "去杭州旅游", "event_time": ["十月"]},
    }

    def __init__(self):
        super().__init__("[]")

    def generate(self, messages, **kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        return json.dumps([e for marker, e in self.EVENTS.items() if marker in prompt])


def _long_message(marker, chat_time):
    return {
        "role": "user",
        "content": marker + "闲" * (3900 - len(marker)),
        "chat_time": chat_time,
    }


def test_a_long_conversation_is_read_in_windows_so_late_events_are_kept():
    jan, jan_later = "2026-01-05T08:00:00+00:00", "2026-01-05T09:00:00+00:00"
    messages = [
        _long_message("北京周会", jan),
        _long_message("", jan),
        _long_message("", jan),
        _long_message("周会", jan_later),
        _long_message("", jan_later),
        _long_message("", jan_later),
        _long_message("杭州", "2026-10-05T08:00:00+00:00"),
    ]
    llm = WindowLLM()

    items = EventExtractor(llm).extract(messages)

    overhead = len(EVENT_EXTRACTION_PROMPT.format(conversation=""))
    assert len(llm.prompts) == 3
    assert all(len(prompt) - overhead <= MAX_WINDOW_CHARS for prompt in llm.prompts)
    by_key = {item.metadata.key: item for item in items}
    # the same-day repeat in the second window is folded, the tail event survives
    assert sorted(by_key) == ["北京出差", "周会", "杭州旅游"]
    assert len(items) == 3
    assert by_key["杭州旅游"].metadata.chat_time == "2026-10-05T08:00:00+00:00"


def test_one_message_longer_than_a_window_is_cut_and_keeps_its_speaker():
    llm = WindowLLM()
    message = {"role": "user", "content": "闲" * 30000 + "杭州"}

    items = EventExtractor(llm).extract([message])

    overhead = len(EVENT_EXTRACTION_PROMPT.format(conversation=""))
    assert len(llm.prompts) == 3
    assert all(len(prompt) - overhead <= MAX_WINDOW_CHARS for prompt in llm.prompts)
    assert all("user: 闲" in prompt for prompt in llm.prompts)
    assert [item.metadata.key for item in items] == ["杭州旅游"]


def _three_windows(tail_time):
    return [
        _long_message("北京", "2026-01-05T08:00:00+00:00"),
        _long_message("", "2026-01-05T08:00:00+00:00"),
        _long_message("", "2026-01-05T08:00:00+00:00"),
        _long_message("周会", "2026-01-05T09:00:00+00:00"),
        _long_message("", "2026-01-05T09:00:00+00:00"),
        _long_message("", "2026-01-05T09:00:00+00:00"),
        _long_message("杭州", tail_time),
    ]


def test_a_failing_window_is_skipped_and_the_others_still_count():
    class FailsOnSecond(WindowLLM):
        def generate(self, messages, **kwargs):
            if len(self.prompts) == 1:
                self.prompts.append(messages[0]["content"])
                raise RuntimeError("llm down")
            return super().generate(messages, **kwargs)

    llm = FailsOnSecond()

    items = EventExtractor(llm).extract(_three_windows("2026-10-05T08:00:00+00:00"))

    assert len(llm.prompts) == 3
    assert sorted(item.metadata.key for item in items) == ["北京出差", "杭州旅游"]


def test_window_cap_drops_the_tail_instead_of_calling_the_llm_without_limit(monkeypatch):
    monkeypatch.setenv("MEMOS_EVENT_MAX_WINDOWS", "2")
    llm = WindowLLM()

    items = EventExtractor(llm).extract(_three_windows("2026-10-05T08:00:00+00:00"))

    assert len(llm.prompts) == 2
    assert sorted(item.metadata.key for item in items) == ["北京出差", "周会"]


def test_a_message_without_text_still_lends_its_time_and_leaves_the_prompt_unchanged():
    llm = FakeLLM(EVENT_JSON)
    messages = [
        {"role": "user", "content": "", "chat_time": "2026-10-20T09:00:00+08:00"},
        {"role": "user", "content": "明天下午三点我在北京和客户开会"},
    ]

    items = EventExtractor(llm).extract(messages)

    assert llm.prompts == [
        EVENT_EXTRACTION_PROMPT.format(conversation="user: 明天下午三点我在北京和客户开会")
    ]
    assert items[0].metadata.chat_time == "2026-10-20T01:00:00+00:00"
