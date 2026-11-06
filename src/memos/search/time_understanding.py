"""Query-side time understanding: does the question look at a moment, and which one.

A cheap gate decides whether the query carries any time wording at all; only then is
one small LLM call made (cached per query and local hour) to turn the wording into
``current`` / ``past`` / ``as_of`` / ``recent`` / ``none`` plus an absolute window.
The LLM never picks memories or versions, that is deterministic code in
``version_select``. When the call is off, slow, broken or unsure, the rule parser in
``time_intent`` answers instead, and below that the query is simply not constrained.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading

from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from memos.context.context import ContextThreadPoolExecutor
from memos.log import get_logger
from memos.mem_reader.temporal import to_utc
from memos.search.time_intent import has_past_intent, parse_time_window, resolve_timezone
from memos.templates.time_intent_prompts import TIME_INTENT_PROMPT, TIME_INTENT_PROMPT_VERSION


logger = get_logger(__name__)

Kind = Literal["current", "past", "as_of", "recent", "none"]
_KINDS = ("current", "past", "as_of", "recent", "none")
_WINDOWED = ("as_of", "recent")

CACHE_SIZE = 1024
MAX_IN_FLIGHT = 4
MIN_CONFIDENCE = 0.5
MAX_WINDOW = timedelta(days=366 * 20)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_CURRENT_WORDS = re.compile(
    r"现在|目前|如今|当前|\bcurrently\b|\bright now\b|\bat present\b|\bnow\b", re.IGNORECASE
)
_RECENT_WORDS = re.compile(r"最近|近期|近来|这阵子|这段时间|\blately\b|\brecently\b", re.IGNORECASE)
_CUE_WORDS = re.compile(
    r"当时|那时|后来|上次|上回|那次|最近|近期|近来|这阵子|这段时间|刚刚|刚才|年初|年底|月初|月底|季度|周末|春节|国庆|中秋|元旦"
    r"|搬家|(?<!\d)(?:19|20)\d{2}(?!\d)"
    r"|\bbefore\b|\bafter\b|\bsince\b|\buntil\b|\bago\b|\blately\b|\brecent(?:ly)?\b|\blast\b|\bearlier\b"
    r"|\bwhen i\b|\bas of\b|\byesterday\b|\btoday\b|\bweek\b|\bmonth\b|\byear\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimeIntent:
    kind: Kind
    start: datetime | None = None
    end: datetime | None = None
    source: str = "gate"
    label: str = ""

    @property
    def has_window(self) -> bool:
        return self.start is not None and self.end is not None


def llm_enabled() -> bool:
    return os.getenv("MEMOS_TIME_INTENT_LLM", "true").strip().lower() in ("1", "true", "yes", "on")


def _timeout() -> float:
    try:
        return max(0.1, float(os.getenv("MEMOS_TIME_INTENT_TIMEOUT_S", "2.0")))
    except ValueError:
        return 2.0


def classify_gate(query: str) -> Literal["none", "current", "cue"]:
    """Free check: no time wording means no constraint; only 'now' wording means current."""
    text = query or ""
    if parse_time_window(text) is not None or has_past_intent(text) or _CUE_WORDS.search(text):
        return "cue"
    return "current" if _CURRENT_WORDS.search(text) else "none"


# ---- cache ---------------------------------------------------------------------------------

_cache: OrderedDict[tuple, TimeIntent] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple) -> TimeIntent | None:
    with _cache_lock:
        intent = _cache.get(key)
        if intent is not None:
            _cache.move_to_end(key)
        return intent


def _cache_put(key: tuple, intent: TimeIntent) -> None:
    with _cache_lock:
        _cache[key] = intent
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


# ---- LLM -----------------------------------------------------------------------------------

_executor: ContextThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
# A timed-out call keeps running in its worker; without a cap, a slow endpoint would pile up
# queued calls (and cost) behind it.
_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)


def _pool() -> ContextThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ContextThreadPoolExecutor(
                max_workers=MAX_IN_FLIGHT, thread_name_prefix="time-intent"
            )
        return _executor


def _ask(llm, prompt: str) -> str | None:
    if not _slots.acquire(blocking=False):
        logger.warning("[TimeIntent] %s LLM calls still in flight, using the rules", MAX_IN_FLIGHT)
        return None
    try:
        future = _pool().submit(llm.generate, [{"role": "user", "content": prompt}])
    except Exception as e:  # noqa: BLE001 - e.g. no thread can be started
        _slots.release()
        logger.warning("[TimeIntent] could not start the LLM call: %s", e)
        return None
    future.add_done_callback(lambda _done: _slots.release())
    try:
        answer = future.result(timeout=_timeout())
    except FutureTimeout:
        future.cancel()
        logger.warning("[TimeIntent] LLM call timed out after %ss", _timeout())
        return None
    except Exception as e:  # noqa: BLE001 - any LLM failure falls back to the rules
        logger.warning("[TimeIntent] LLM call failed: %s", e)
        return None
    return answer if isinstance(answer, str) else None


def _validate(payload: object) -> TimeIntent | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("intent") or "").strip().lower()
    if kind not in _KINDS:
        return None
    try:
        confidence = float(payload.get("confidence", 1.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or confidence < MIN_CONFIDENCE:
        return None
    if kind not in _WINDOWED:
        return TimeIntent(kind, source="llm")
    start = to_utc(payload.get("start"), end_of_day=False)
    end = to_utc(payload.get("end") or payload.get("start"), end_of_day=True)
    if start is None or end is None or end < start or end - start > MAX_WINDOW:
        return None
    return TimeIntent(kind, start, end, "llm")


def _intent_from_llm(llm, query: str, local_now: datetime) -> TimeIntent | None:
    prompt = TIME_INTENT_PROMPT.format(
        now=local_now.isoformat(timespec="minutes"),
        weekday=_WEEKDAYS[local_now.weekday()],
        timezone=str(local_now.tzinfo),
        query=query.strip(),
    )
    answer = _ask(llm, prompt)
    if not answer:
        return None
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if match is None:
        logger.warning("[TimeIntent] LLM answer has no JSON object")
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        logger.warning("[TimeIntent] LLM answer is not valid JSON")
        return None
    intent = _validate(payload)
    if intent is None:
        logger.warning("[TimeIntent] LLM answer rejected: %s", match.group(0)[:200])
    return intent


# ---- rules (fallback) ----------------------------------------------------------------------


def _intent_from_rules(query: str, local_now: datetime) -> TimeIntent:
    window = parse_time_window(query, now=local_now, tz=local_now.tzinfo)
    if window is not None:
        return TimeIntent(
            "as_of",
            window.start.astimezone(UTC),
            window.end.astimezone(UTC),
            "rules",
            window.label,
        )
    if has_past_intent(query):
        return TimeIntent("past", source="rules")
    if _RECENT_WORDS.search(query):
        # No window to filter by, only a lean toward the newer hits (see version_select).
        return TimeIntent("recent", source="rules")
    return TimeIntent("none", source="rules")


def understand_query(query: str, llm=None, *, now: datetime | None = None) -> TimeIntent:
    """Time intent of ``query``. Never raises; every failure degrades toward 'no constraint'."""
    gate = classify_gate(query)
    if gate != "cue":
        return TimeIntent("current" if gate == "current" else "none", source="gate")
    local_now = (now or datetime.now(UTC)).astimezone(resolve_timezone())
    if llm is not None and llm_enabled():
        key = (
            " ".join(query.split()).lower(),
            local_now.strftime("%Y-%m-%dT%H"),
            str(local_now.tzinfo),
            TIME_INTENT_PROMPT_VERSION,
        )
        cached = _cache_get(key)
        if cached is not None:
            return replace(cached, source="cache")
        intent = _intent_from_llm(llm, query, local_now)
        if intent is not None:
            _cache_put(key, intent)
            return intent
    return _intent_from_rules(query, local_now)
