"""Rule-based time intent for search queries (no LLM call).

``parse_time_window`` turns common Chinese / English time expressions ("上周",
"去年3月", "last 7 days", "2026-03-13") into a UTC window. ``build_time_filter``
turns the window into a graph filter that matches a memory when either its
creation time or its event time (``chat_time``) falls inside it.

A query with no recognised expression yields ``None`` so callers never add a
filter they cannot justify.
"""

from __future__ import annotations

import os
import re

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_NUM = r"(\d{1,3}|[零〇一二两三四五六七八九十]{1,3})"
_MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_NAME_RE = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
_YEAR_WORDS = {"今年": 0, "去年": -1, "前年": -2}

_PAST_INTENT = re.compile(
    r"以前|曾经|曾在|曾住|从前|原先|原来|早先|早些时候|之前|过去(?!\s*" + _NUM + r")"
    r"|\bused to\b|\bpreviously\b|\bformerly\b|\bback then\b|\bin the past\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    label: str

    def iso(self) -> tuple[str, str]:
        return (
            self.start.astimezone(UTC).isoformat(),
            self.end.astimezone(UTC).isoformat(),
        )


def resolve_timezone(name: str | None = None) -> tzinfo:
    name = (name or os.getenv("MEMOS_QUERY_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def time_aware_enabled(request_flag: bool | None = None) -> bool:
    """A per-request flag wins; otherwise ``SEARCH_TIME_AWARE`` (default off)."""
    if request_flag is not None:
        return bool(request_flag)
    return os.getenv("SEARCH_TIME_AWARE", "false").strip().lower() == "true"


def has_past_intent(query: str) -> bool:
    """True for queries that ask about an earlier state ("我以前住哪")."""
    return bool(_PAST_INTENT.search(query or ""))


def parse_time_window(
    query: str, *, now: datetime | None = None, tz: tzinfo | None = None
) -> TimeWindow | None:
    """Return the window a query points at, or ``None`` when no rule matches."""
    if not query or not query.strip():
        return None
    tz = tz or resolve_timezone()
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    text = query.strip().lower()

    for rule in (
        _explicit_date,
        _year_word_month,
        _month_name,
        _relative_day,
        _relative_span,
        _recent_span,
        _days_ago,
        _month_day,
        _bare_month,
    ):
        window = rule(text, local_now)
        if window is not None:
            return window
    return None


def build_time_filter(window: TimeWindow) -> dict[str, Any]:
    start, end = window.iso()
    return {
        "or": [
            {"created_at": {"gte": start, "lte": end}},
            {"chat_time": {"gte": start, "lte": end}},
        ]
    }


def merge_time_filter(existing: dict[str, Any] | None, window: TimeWindow) -> dict[str, Any]:
    """AND the time window onto a caller filter without mutating it."""
    time_filter = build_time_filter(window)
    if not existing:
        return time_filter
    return {"and": [existing, time_filter]}


# ---- rules -----------------------------------------------------------------------------


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(token) == 1:
        return _CN_DIGITS.get(token)
    return None


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _window(start: datetime, end_exclusive: datetime, label: str) -> TimeWindow:
    return TimeWindow(start, end_exclusive - timedelta(microseconds=1), label)


def _month_bounds(year: int, month: int, tz: tzinfo) -> tuple[datetime, datetime] | None:
    if not 1 <= month <= 12:
        return None
    start = datetime(year, month, 1, tzinfo=tz)
    nxt = (
        datetime(year + 1, 1, 1, tzinfo=tz)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=tz)
    )
    return start, nxt


def _day_window(year: int, month: int, day: int, tz: tzinfo, label: str) -> TimeWindow | None:
    try:
        start = datetime(year, month, day, tzinfo=tz)
    except ValueError:
        return None
    return _window(start, start + timedelta(days=1), label)


def _month_window(year: int, month: int, tz: tzinfo, label: str) -> TimeWindow | None:
    bounds = _month_bounds(year, month, tz)
    return _window(*bounds, label) if bounds else None


def _past_year_for(month: int, local_now: datetime) -> int:
    # A bare month never means the future: "3月" asked in January is last March.
    return local_now.year if month <= local_now.month else local_now.year - 1


def _explicit_date(text: str, now: datetime) -> TimeWindow | None:
    tz = now.tzinfo
    match = re.search(
        r"(?<!\d)(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?", text
    )
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _day_window(year, month, day, tz, match.group(0))
    match = re.search(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    if match:
        return _month_window(int(match.group(1)), int(match.group(2)), tz, match.group(0))
    match = re.search(r"(?<!\d)(\d{4})\s*年(?!\s*\d)", text)
    if match and 1970 <= int(match.group(1)) <= 2100:
        year = int(match.group(1))
        return _window(
            datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz), match.group(0)
        )
    return None


def _year_word_month(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(r"(今年|去年|前年)\s*(\d{1,2})\s*月", text)
    if not match:
        return None
    year = now.year + _YEAR_WORDS[match.group(1)]
    return _month_window(year, int(match.group(2)), now.tzinfo, match.group(0))


def _month_name(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(rf"\b({_MONTH_NAME_RE})\b\.?(?:\s+(\d{{4}}))?", text)
    if not match:
        return None
    month = _MONTH_NAMES[match.group(1)]
    if match.group(1) == "may" and not re.search(r"\bin may\b|\bmay\s+\d{4}\b", text):
        return None
    year = int(match.group(2)) if match.group(2) else _past_year_for(month, now)
    return _month_window(year, month, now.tzinfo, match.group(0))


def _relative_day(text: str, now: datetime) -> TimeWindow | None:
    today = _day_start(now)
    for pattern, offset in (
        (r"大前天", -3),
        (r"前天|the day before yesterday", -2),
        (r"昨天|昨日|yesterday", -1),
        (r"今天|今日|\btoday\b", 0),
    ):
        match = re.search(pattern, text)
        if match:
            start = today + timedelta(days=offset)
            return _window(start, start + timedelta(days=1), match.group(0))
    return None


def _relative_span(text: str, now: datetime) -> TimeWindow | None:
    tz = now.tzinfo
    week_start = _day_start(now) - timedelta(days=now.weekday())
    for pattern, weeks in (
        (r"上上(?:个)?(?:周|星期)", -2),
        (r"上(?:个)?(?:周|星期)(?!期)|last week", -1),
        (r"本(?:周|星期)|这(?:个)?(?:周|星期)|this week", 0),
    ):
        match = re.search(pattern, text)
        if match:
            start = week_start + timedelta(weeks=weeks)
            return _window(start, start + timedelta(weeks=1), match.group(0))

    for pattern, months in (
        (r"上上(?:个)?月", -2),
        (r"上(?:个)?月(?!份?\d)|last month", -1),
        (r"本月|这(?:个)?月|this month", 0),
    ):
        match = re.search(pattern, text)
        if match:
            index = now.year * 12 + now.month - 1 + months
            return _month_window(index // 12, index % 12 + 1, tz, match.group(0))

    for pattern, years in (
        (r"去年|last year", -1),
        (r"前年", -2),
        (r"今年|this year", 0),
    ):
        match = re.search(pattern, text)
        if match:
            year = now.year + years
            return _window(
                datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz), match.group(0)
            )
    return None


_ZH_UNITS = {"天": "days", "日": "days", "周": "weeks", "星期": "weeks", "小时": "hours"}
_EN_UNITS = {"hour": "hours", "day": "days", "week": "weeks", "month": "months", "year": "years"}


def _shift(now: datetime, unit: str, count: int) -> datetime:
    if unit == "months":
        index = now.year * 12 + now.month - 1 - count
        year, month = index // 12, index % 12 + 1
        day = min(now.day, (_month_bounds(year, month, now.tzinfo)[1] - timedelta(days=1)).day)
        return now.replace(year=year, month=month, day=day)
    if unit == "years":
        try:
            return now.replace(year=now.year - count)
        except ValueError:
            return now.replace(year=now.year - count, day=28)
    return now - timedelta(**{unit: count})


def _recent_span(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(
        rf"(?:最近|近|过去|这)\s*{_NUM}\s*(?:个)?\s*(天|日|周|星期|小时|月|年)(?:内|以来|里)?", text
    )
    if match:
        count = _to_int(match.group(1))
        unit = match.group(2)
        if count and count > 0:
            normalized = {"月": "months", "年": "years"}.get(unit) or _ZH_UNITS[unit]
            return TimeWindow(_shift(now, normalized, count), now, match.group(0))
    match = re.search(r"(?:last|past)\s+(\d{1,3})\s+(hour|day|week|month|year)s?", text)
    if match and int(match.group(1)) > 0:
        return TimeWindow(
            _shift(now, _EN_UNITS[match.group(2)], int(match.group(1))), now, match.group(0)
        )
    return None


def _days_ago(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(rf"{_NUM}\s*(?:天|日)前", text) or re.search(r"(\d{1,3})\s+days?\s+ago", text)
    if not match:
        return None
    count = _to_int(match.group(1))
    if not count or count <= 0:
        return None
    start = _day_start(now) - timedelta(days=count)
    return _window(start, start + timedelta(days=1), match.group(0))


def _month_day(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    return _day_window(_past_year_for(month, now), month, day, now.tzinfo, match.group(0))


def _bare_month(text: str, now: datetime) -> TimeWindow | None:
    match = re.search(r"(?<![\d个])(\d{1,2})\s*月(?:份)?(?!\s*\d)", text)
    if not match:
        return None
    month = int(match.group(1))
    return _month_window(_past_year_for(month, now), month, now.tzinfo, match.group(0))
