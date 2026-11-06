"""Temporal fields of extracted memories: ``memory_form`` and the event's absolute time.

The extraction call already sees the message timestamps, so it is the only place
where "yesterday" can be turned into a date. Instead of a second LLM call, a short
section is appended to the chat extraction prompt (like ``custom_extract_prompt``,
so it also applies when the base prompt is replaced) and the two extra fields are
read back here. Anything missing or malformed means "state, no event time", which
is exactly what every node written before this existed is.
"""

from __future__ import annotations

import os
import re

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from memos.search.time_intent import resolve_timezone
from memos.utils import parse_datetime


_CJK = re.compile(r"[一-鿿]")
_DATE_ONLY = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")

_SECTION_EN = (
    '\n\n# Temporal fields (add to every item of "memory list")\n'
    '- "memory_form": "event" for something that happened, or is scheduled, at a specific '
    'time and does not replace anything (a trip, a meeting, a purchase, a visit); "state" for a '
    "fact about the user that stays true until it changes (where the user lives or works, a "
    'preference, a relationship, a plan that is still pending). When unsure, use "state".\n'
    '- "event_time": only for "event" items: {"start": <ISO 8601 date or date-time>, '
    '"end": <ISO 8601 date or date-time, same as start for a single moment>}. Resolve relative '
    'expressions such as "yesterday" or "last week" with the message timestamp. Omit the '
    "field when the time cannot be determined.\n"
)
_SECTION_ZH = (
    '\n\n# 时间字段（加到 "memory list" 的每一条里）\n'
    '- "memory_form"：某个具体时间发生（或已排定）、不会取代任何旧事实的事用 "event"（一次出差、'
    '一次会议、一次购买、一次拜访）；会一直成立、直到被新情况改变的关于用户的事实用 "state"（住在哪、'
    '在哪工作、偏好、关系、尚未完成的计划）。拿不准就用 "state"。\n'
    '- "event_time"：仅 "event" 填写：{"start": <ISO 8601 日期或日期时间>, "end": <ISO 8601 '
    '日期或日期时间，单个时刻与 start 相同>}。"昨天""上周"等相对时间按消息时间戳换算成绝对时间；'
    "无法确定就不要写这个字段。\n"
)


def extraction_enabled() -> bool:
    return os.getenv("MEMOS_EXTRACT_TEMPORAL", "true").strip().lower() in ("1", "true", "yes", "on")


def append_temporal_section(prompt: str) -> str:
    """Ask the extraction LLM for ``memory_form`` / ``event_time`` (no-op when switched off)."""
    if not extraction_enabled():
        return prompt
    return prompt + (_SECTION_ZH if _CJK.search(prompt) else _SECTION_EN)


def to_utc(value: Any, *, end_of_day: bool) -> datetime | None:
    """Parse an LLM-written time as local wall-clock; a bare date covers the whole local day."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    tz = resolve_timezone()
    if _DATE_ONLY.match(text):
        year, month, day = (int(p) for p in text.split("-"))
        try:
            moment = datetime(year, month, day, tzinfo=tz)
        except ValueError:
            return None
        if end_of_day:
            moment = moment + timedelta(days=1) - timedelta(seconds=1)
        return moment.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        # parse_datetime tags the naive fallback formats as UTC; the LLM wrote local time.
        fallback = parse_datetime(text)
        if fallback is None:
            return None
        parsed = fallback.replace(tzinfo=None)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def temporal_fields(raw: Mapping[str, Any]) -> dict[str, str]:
    """Metadata keyword arguments for one extracted memory dict; ``{}`` means "state"."""
    form = str(raw.get("memory_form") or "").strip().lower()
    if form not in ("state", "event"):
        return {}
    fields: dict[str, str] = {"memory_form": form}
    if form != "event":
        return fields
    event_time = raw.get("event_time")
    if isinstance(event_time, str):
        event_time = {"start": event_time, "end": event_time}
    if not isinstance(event_time, Mapping):
        return fields
    start = to_utc(event_time.get("start"), end_of_day=False)
    end = to_utc(event_time.get("end") or event_time.get("start"), end_of_day=True)
    if start is None and end is None:
        return fields
    start = start or end
    end = end or start
    if end < start:
        start, end = end, start
    fields["event_time_start"] = start.isoformat()
    fields["event_time_end"] = end.isoformat()
    return fields
