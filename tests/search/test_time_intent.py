"""Rule-based time intent for search queries."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from memos.search.time_intent import (
    build_time_filter,
    has_past_intent,
    merge_time_filter,
    parse_time_window,
    resolve_timezone,
    time_aware_enabled,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
# Wednesday 2026-10-21 14:30 in Shanghai.
NOW = datetime(2026, 10, 21, 14, 30, tzinfo=SHANGHAI)


def window(query):
    found = parse_time_window(query, now=NOW, tz=SHANGHAI)
    return found.iso() if found else None


@pytest.mark.parametrize(
    "query,expected",
    [
        ("今天开了什么会", ("2026-10-20T16:00:00+00:00", "2026-10-21T15:59:59.999999+00:00")),
        (
            "what did I do yesterday",
            ("2026-10-19T16:00:00+00:00", "2026-10-20T15:59:59.999999+00:00"),
        ),
        ("前天吃了什么", ("2026-10-18T16:00:00+00:00", "2026-10-19T15:59:59.999999+00:00")),
        ("上周的评审", ("2026-10-11T16:00:00+00:00", "2026-10-18T15:59:59.999999+00:00")),
        ("last week's review", ("2026-10-11T16:00:00+00:00", "2026-10-18T15:59:59.999999+00:00")),
        ("本周计划", ("2026-10-18T16:00:00+00:00", "2026-10-25T15:59:59.999999+00:00")),
        ("上个月的账单", ("2026-08-31T16:00:00+00:00", "2026-09-30T15:59:59.999999+00:00")),
        ("this month goals", ("2026-09-30T16:00:00+00:00", "2026-10-31T15:59:59.999999+00:00")),
        ("去年的总结", ("2024-12-31T16:00:00+00:00", "2025-12-31T15:59:59.999999+00:00")),
        ("去年3月的旅行", ("2025-02-28T16:00:00+00:00", "2025-03-31T15:59:59.999999+00:00")),
        ("2026年3月13日的会议", ("2026-03-12T16:00:00+00:00", "2026-03-13T15:59:59.999999+00:00")),
        (
            "meeting on 2026-03-13",
            ("2026-03-12T16:00:00+00:00", "2026-03-13T15:59:59.999999+00:00"),
        ),
        ("2025年的目标", ("2024-12-31T16:00:00+00:00", "2025-12-31T15:59:59.999999+00:00")),
        ("2026年3月的事", ("2026-02-28T16:00:00+00:00", "2026-03-31T15:59:59.999999+00:00")),
        ("三天前的会", ("2026-10-17T16:00:00+00:00", "2026-10-18T15:59:59.999999+00:00")),
        ("2 days ago", ("2026-10-18T16:00:00+00:00", "2026-10-19T15:59:59.999999+00:00")),
        ("March 2026 trip", ("2026-02-28T16:00:00+00:00", "2026-03-31T15:59:59.999999+00:00")),
    ],
)
def test_expressions_resolve_to_a_utc_window(query, expected):
    assert window(query) == expected


@pytest.mark.parametrize(
    "query,start",
    [
        ("最近7天的事", "2026-10-14T06:30:00+00:00"),
        ("过去两周", "2026-10-07T06:30:00+00:00"),
        ("近三个月", "2026-07-21T06:30:00+00:00"),
        ("最近十天", "2026-10-11T06:30:00+00:00"),
        ("last 7 days", "2026-10-14T06:30:00+00:00"),
        ("past 2 hours", "2026-10-21T04:30:00+00:00"),
    ],
)
def test_trailing_spans_end_now(query, start):
    assert window(query) == (start, "2026-10-21T06:30:00+00:00")


def test_a_bare_month_never_points_to_the_future():
    this_year = window("3月的会议")
    last_year = window("12月的会议")

    assert this_year[0].startswith("2026-02-28")
    assert last_year[0].startswith("2025-11-30")


def test_month_day_without_a_year_uses_the_latest_past_date():
    assert window("3月13日") == ("2026-03-12T16:00:00+00:00", "2026-03-13T15:59:59.999999+00:00")
    assert window("12月5日")[0].startswith("2025-12-04")


@pytest.mark.parametrize(
    "query",
    ["我喜欢吃什么", "最近的会议", "show my preferences", "上周期的数据", "我养了3个月的猫", ""],
)
def test_queries_without_a_time_expression_are_left_alone(query):
    assert window(query) is None


def test_invalid_calendar_dates_are_ignored():
    assert window("2026年2月30日") is None


def test_may_is_only_a_month_when_it_reads_like_one():
    assert window("you may like tea") is None
    assert window("trip in May 2025") == (
        "2025-04-30T16:00:00+00:00",
        "2025-05-31T15:59:59.999999+00:00",
    )


def test_month_shift_clamps_the_day():
    now = datetime(2026, 3, 31, 12, 0, tzinfo=SHANGHAI)

    found = parse_time_window("最近一个月", now=now, tz=SHANGHAI)

    assert found.start == datetime(2026, 2, 28, 12, 0, tzinfo=SHANGHAI)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("我以前住哪", True),
        ("where did I live before, I used to", True),
        ("之前的方案是什么", True),
        ("过去三天做了什么", False),
        ("我现在住哪", False),
    ],
)
def test_past_intent(query, expected):
    assert has_past_intent(query) is expected


def test_filter_matches_creation_or_event_time():
    found = parse_time_window("昨天", now=NOW, tz=SHANGHAI)

    start, end = found.iso()
    assert build_time_filter(found) == {
        "or": [
            {"created_at": {"gte": start, "lte": end}},
            {"chat_time": {"gte": start, "lte": end}},
        ]
    }


def test_merge_keeps_the_caller_filter_and_does_not_mutate_it():
    found = parse_time_window("昨天", now=NOW, tz=SHANGHAI)
    caller = {"tags": {"contains": "travel"}}

    merged = merge_time_filter(caller, found)

    assert merged["and"][0] == {"tags": {"contains": "travel"}}
    assert merged["and"][1] == build_time_filter(found)
    assert caller == {"tags": {"contains": "travel"}}
    assert merge_time_filter(None, found) == build_time_filter(found)


def test_switch_defaults_to_off_and_request_flag_wins(monkeypatch):
    monkeypatch.delenv("SEARCH_TIME_AWARE", raising=False)
    assert time_aware_enabled() is False
    assert time_aware_enabled(True) is True

    monkeypatch.setenv("SEARCH_TIME_AWARE", "true")
    assert time_aware_enabled() is True
    assert time_aware_enabled(False) is False


def test_timezone_comes_from_env_and_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv("MEMOS_QUERY_TIMEZONE", "America/New_York")
    assert resolve_timezone().key == "America/New_York"

    monkeypatch.setenv("MEMOS_QUERY_TIMEZONE", "Not/AZone")
    assert resolve_timezone() is UTC
