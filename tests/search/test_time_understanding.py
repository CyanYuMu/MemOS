import json
import threading

from datetime import UTC, datetime

import pytest

from memos.search import time_understanding as tu
from memos.search.time_understanding import classify_gate, understand_query
from memos.templates.time_intent_prompts import TIME_INTENT_PROMPT


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class FakeLLM:
    def __init__(self, *answers, delay=None):
        self.answers = list(answers)
        self.prompts = []
        self.delay = delay

    def generate(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        if self.delay is not None:
            self.delay.wait(5)
        answer = self.answers[min(len(self.prompts), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    @property
    def calls(self):
        return len(self.prompts)


def _as_of(start, end, confidence=0.9):
    return {"intent": "as_of", "start": start, "end": end, "confidence": confidence}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("MEMOS_TIME_INTENT_LLM", "MEMOS_TIME_INTENT_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMOS_QUERY_TIMEZONE", "Asia/Shanghai")
    tu.clear_cache()


# ---- gate ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我喜欢什么", "none"),
        ("What does the user like?", "none"),
        ("", "none"),
        ("我现在住哪", "current"),
        ("where do I live right now", "current"),
        ("2025年6月我住哪", "cue"),
        ("我以前住哪", "cue"),
        ("上周的评审", "cue"),
        ("搬家之后我住哪", "cue"),
        ("what did I do last month", "cue"),
        ("when I was in Beijing", "cue"),
        ("2024 trip plans", "cue"),
        ("现在还记得当时的事吗", "cue"),
    ],
)
def test_gate(query, expected):
    assert classify_gate(query) == expected


def test_no_cue_never_reaches_the_llm():
    llm = FakeLLM(RuntimeError("must not be called"))

    assert understand_query("我喜欢什么", llm, now=NOW).kind == "none"
    assert understand_query("我现在住哪", llm, now=NOW).kind == "current"
    assert llm.calls == 0


# ---- LLM answers ----------------------------------------------------------------------------


def test_the_llm_window_is_read_as_local_time_and_returned_in_utc():
    llm = FakeLLM(_as_of("2025-06-01", "2025-06-30"))

    intent = understand_query("2025年6月我住哪", llm, now=NOW)

    assert (intent.kind, intent.source) == ("as_of", "llm")
    assert intent.start == datetime(2025, 5, 31, 16, 0, tzinfo=UTC)
    assert intent.end == datetime(2025, 6, 30, 15, 59, 59, tzinfo=UTC)


def test_the_prompt_carries_the_local_date_the_zone_and_the_query():
    llm = FakeLLM({"intent": "past", "confidence": 0.9})

    understand_query("我以前住哪 {x}", llm, now=NOW)

    prompt = llm.prompts[0]
    assert "2026-10-21T20:00" in prompt and "Asia/Shanghai" in prompt and "Wednesday" in prompt
    assert "我以前住哪 {x}" in prompt
    assert TIME_INTENT_PROMPT.count("{query}") == 1


def test_the_llm_can_veto_a_cue_word():
    llm = FakeLLM({"intent": "none", "confidence": 0.95})

    intent = understand_query("上次的结论是什么", llm, now=NOW)

    assert (intent.kind, intent.source) == ("none", "llm")


def test_json_inside_prose_or_a_code_fence_is_accepted():
    answer = (
        'Sure!\n```json\n{"intent": "past", "start": null, "end": null, "confidence": 0.8}\n```'
    )
    llm = FakeLLM(answer)

    assert understand_query("我以前住哪", llm, now=NOW).kind == "past"


def test_a_single_day_needs_no_end():
    llm = FakeLLM({"intent": "as_of", "start": "2025-06-10", "confidence": 0.9})

    intent = understand_query("2025年6月10日那天", llm, now=NOW)

    assert intent.start == datetime(2025, 6, 9, 16, 0, tzinfo=UTC)
    assert intent.end == datetime(2025, 6, 10, 15, 59, 59, tzinfo=UTC)


# ---- cache ----------------------------------------------------------------------------------


def test_the_same_query_in_the_same_hour_is_asked_once():
    llm = FakeLLM(_as_of("2025-06-01", "2025-06-30"))

    first = understand_query("2025年6月我住哪", llm, now=NOW)
    second = understand_query("  2025年6月我住哪 ", llm, now=NOW)

    assert llm.calls == 1
    assert second.source == "cache" and (second.start, second.end) == (first.start, first.end)


def test_a_new_local_hour_asks_again():
    llm = FakeLLM({"intent": "recent", "start": "2026-10-14", "end": "2026-10-21", "confidence": 1})

    understand_query("刚才发生了什么", llm, now=NOW)
    understand_query("刚才发生了什么", llm, now=datetime(2026, 10, 21, 13, 0, tzinfo=UTC))

    assert llm.calls == 2


def test_a_failure_is_not_cached():
    llm = FakeLLM(RuntimeError("down"), _as_of("2025-06-01", "2025-06-30"))

    assert understand_query("2025年6月我住哪", llm, now=NOW).source == "rules"
    assert understand_query("2025年6月我住哪", llm, now=NOW).source == "llm"
    assert llm.calls == 2


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(tu, "CACHE_SIZE", 2)
    llm = FakeLLM({"intent": "past", "confidence": 0.9})

    for query in ("我以前住哪", "我以前吃什么", "我以前学什么"):
        understand_query(query, llm, now=NOW)
    understand_query("我以前住哪", llm, now=NOW)

    assert llm.calls == 4


# ---- fallback chain: LLM -> rules -> no constraint ---------------------------------------------


def test_a_slow_llm_falls_back_to_the_rules(monkeypatch):
    monkeypatch.setenv("MEMOS_TIME_INTENT_TIMEOUT_S", "0.1")
    release = threading.Event()
    llm = FakeLLM(_as_of("2020-01-01", "2020-01-02"), delay=release)

    try:
        started = datetime.now(UTC)
        intent = understand_query("2025年6月我住哪", llm, now=NOW)
        elapsed = (datetime.now(UTC) - started).total_seconds()
    finally:
        release.set()

    assert (intent.kind, intent.source) == ("as_of", "rules")
    assert intent.start == datetime(2025, 5, 31, 16, 0, tzinfo=UTC)
    assert elapsed < 2


def test_calls_still_in_flight_are_capped_and_a_dead_pool_does_not_raise(monkeypatch):
    llm = FakeLLM(_as_of("2020-01-01", "2020-01-02"))
    for _ in range(tu.MAX_IN_FLIGHT):
        assert tu._slots.acquire(blocking=False)
    try:
        saturated = understand_query("2025年6月我住哪", llm, now=NOW)
    finally:
        for _ in range(tu.MAX_IN_FLIGHT):
            tu._slots.release()
    monkeypatch.setattr(tu, "_pool", lambda: (_ for _ in ()).throw(RuntimeError("no thread")))
    dead = understand_query("2025年6月我住哪", llm, now=NOW)

    assert llm.calls == 0
    assert saturated.source == dead.source == "rules"
    assert tu._slots.acquire(blocking=False)
    tu._slots.release()


def test_a_broken_llm_falls_back_to_the_rules():
    llm = FakeLLM(RuntimeError("boom"))

    intent = understand_query("2025年6月我住哪", llm, now=NOW)

    assert (intent.kind, intent.source) == ("as_of", "rules")


@pytest.mark.parametrize(
    "answer",
    [
        "no json here",
        "{not json}",
        {"intent": "sometime", "confidence": 0.9},
        _as_of("2025-06-01", "2025-06-30", confidence=0.3),
        _as_of("2025-06-30", "2025-06-01"),
        _as_of("1900-01-01", "2025-06-01"),
        _as_of(None, None),
        _as_of("not a date", "2025-06-01"),
        {"intent": "as_of", "start": "2025-06-01", "end": "2025-06-30", "confidence": "high"},
        [1, 2],
    ],
)
def test_an_unusable_answer_is_treated_as_no_answer(answer):
    llm = FakeLLM(answer)

    intent = understand_query("2025年6月我住哪", llm, now=NOW)

    assert (intent.kind, intent.source) == ("as_of", "rules")


def test_rules_know_a_past_question_without_a_date():
    intent = understand_query("我以前住哪", FakeLLM(RuntimeError("x")), now=NOW)

    assert (intent.kind, intent.source) == ("past", "rules")


def test_below_the_rules_the_query_is_not_constrained():
    intent = understand_query("上次的结论是什么", FakeLLM(RuntimeError("x")), now=NOW)

    assert (intent.kind, intent.source) == ("none", "rules")


def test_without_an_llm_the_rules_answer():
    intent = understand_query("2025年6月我住哪", None, now=NOW)

    assert (intent.kind, intent.source) == ("as_of", "rules")


@pytest.mark.parametrize("value", ["false", "0", "off"])
def test_the_llm_can_be_switched_off(monkeypatch, value):
    monkeypatch.setenv("MEMOS_TIME_INTENT_LLM", value)
    llm = FakeLLM(_as_of("2020-01-01", "2020-01-02"))

    intent = understand_query("2025年6月我住哪", llm, now=NOW)

    assert llm.calls == 0 and intent.source == "rules"


def test_a_bad_timeout_setting_uses_the_default(monkeypatch):
    monkeypatch.setenv("MEMOS_TIME_INTENT_TIMEOUT_S", "soon")
    llm = FakeLLM(_as_of("2025-06-01", "2025-06-30"))

    assert understand_query("2025年6月我住哪", llm, now=NOW).source == "llm"
