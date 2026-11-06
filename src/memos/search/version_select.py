"""Pick what a search hit says about the moment a query asks for.

Deterministic, no LLM: ``time_understanding`` turns the wording into an intent, this
module applies it to the hits that came back. A hit is one head node; its earlier
versions live in ``metadata.history`` with the period each was valid, so the same hit
can answer "now" with its text and "June 2025" with an earlier version's text.

Only long-term and user memories are judged; documents, skills, tools, preferences
and the rest are not time-bound and pass through untouched. When nothing is judged
to match, the original hits are returned (trimmed) so a guessed window never turns a
searchable query into an empty answer.
"""

from __future__ import annotations

import json
import math
import os

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from memos.utils import normalize_to_utc_iso, parse_datetime


if TYPE_CHECKING:
    from memos.search.time_understanding import TimeIntent


CANDIDATE_CAP = 30
RERANK_WEIGHT_ENV = "SEARCH_TIME_RERANK_WEIGHT"
DEFAULT_RERANK_WEIGHT = 0.25
# Without a relevance score the position stands in for one; adjacent hits are assumed nearly equal.
_POSITION_STEP = 0.02
JUDGED_TYPES = ("LongTermMemory", "UserMemory")
_FAR_PAST = datetime.min.replace(tzinfo=UTC)
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def widened_top_k(top_k: int) -> int:
    """Candidates to retrieve so that version selection still has ``top_k`` to return."""
    return top_k if top_k >= CANDIDATE_CAP else min(top_k * 3, CANDIDATE_CAP)


def _parse(value: Any) -> datetime | None:
    iso = normalize_to_utc_iso(value)
    return parse_datetime(iso) if iso else None


def _load(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


@dataclass(frozen=True)
class _Version:
    text: str
    start: datetime | None
    end: datetime | None
    number: int | None = None
    is_head: bool = False


def _head_start(meta: dict[str, Any]) -> datetime | None:
    return (
        _parse(meta.get("valid_from"))
        or _parse(meta.get("chat_time"))
        or _parse(meta.get("created_at"))
    )


def _versions(memory: dict[str, Any], meta: dict[str, Any]) -> list[_Version]:
    """Head plus history, oldest first; a version without an end ends where the next begins."""
    versions: list[_Version] = []
    for raw in _load(meta.get("history")) or []:
        entry = _load(raw)
        if not isinstance(entry, dict) or not isinstance(entry.get("memory"), str):
            continue
        spec = _load(entry.get("timespec")) or {}
        if not isinstance(spec, dict):
            spec = {}
        number = entry.get("version")
        versions.append(
            _Version(
                entry["memory"],
                _parse(spec.get("valid_from")) or _parse(entry.get("created_at")),
                _parse(spec.get("valid_to")),
                number if isinstance(number, int) else None,
            )
        )
    head_start = _head_start(meta)
    versions.sort(key=lambda v: v.start or _FAR_PAST)
    versions.append(
        _Version(memory.get("memory") or "", head_start, _parse(meta.get("valid_to")), None, True)
    )
    closed: list[_Version] = []
    for index, version in enumerate(versions):
        end = version.end
        if end is None and not version.is_head:
            end = next((n.start for n in versions[index + 1 :] if n.start), None)
        closed.append(_Version(version.text, version.start, end, version.number, version.is_head))
    return closed


def _event_interval(meta: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = _parse(meta.get("event_time_start"))
    end = _parse(meta.get("event_time_end"))
    if start is None and end is None:
        start = end = _parse(meta.get("chat_time")) or _parse(meta.get("created_at"))
    elif start is None or end is None:
        start = end = start or end
    if start is None or end is None:
        return None
    return (start, end) if start <= end else (end, start)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


@dataclass(frozen=True)
class _Hit:
    """A hit as it will be returned, plus what the time rerank needs to know about it."""

    memory: Any
    moment: datetime | None = None
    interval: tuple[datetime | None, datetime | None] | None = None
    judged: bool = False


def _pick(memory: dict[str, Any], intent: TimeIntent) -> _Hit | None:
    """The hit as it should read for ``intent`` with its time, or ``None`` when it does not fit."""
    meta = memory.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    window_start, window_end = intent.start, intent.end
    if meta.get("memory_form") == "event":
        interval = _event_interval(meta)
        if interval is None or interval[0] > window_end or interval[1] < window_start:
            return None
        return _Hit(memory, interval[0], interval, True)

    chosen: _Version | None = None
    for version in _versions(memory, meta):
        if intent.kind == "recent":
            hit = version.start is not None and window_start <= version.start <= window_end
        else:
            hit = (version.start is None or version.start <= window_end) and (
                version.end is None or version.end > window_start
            )
        if hit and (chosen is None or (version.start or _FAR_PAST) >= (chosen.start or _FAR_PAST)):
            chosen = version
    if chosen is None:
        return None
    interval = (chosen.start, chosen.end)
    if chosen.is_head:
        return _Hit(memory, chosen.start, interval, True)
    picked = dict(memory)
    picked["memory"] = chosen.text
    picked["metadata"] = {
        **meta,
        "current_memory": memory.get("memory"),
        "matched_version": {
            "version": chosen.number,
            "valid_from": _iso(chosen.start),
            "valid_to": _iso(chosen.end),
        },
    }
    return _Hit(picked, chosen.start, interval, True)


def _head_hit(memory: Any) -> _Hit:
    """A hit read as it stands, with the time its current text began (for the soft recency)."""
    if not _is_judged(memory):
        return _Hit(memory)
    meta = memory["metadata"]
    if meta.get("memory_form") == "event":
        interval = _event_interval(meta)
        return _Hit(memory, interval[0] if interval else None, interval, True)
    return _Hit(memory, _head_start(meta), None, True)


def _is_judged(memory: Any) -> bool:
    if not isinstance(memory, dict):
        return False
    meta = memory.get("metadata")
    return isinstance(meta, dict) and meta.get("memory_type") in JUDGED_TYPES


def _expired_head(memory: dict[str, Any], now: datetime) -> bool:
    meta = memory.get("metadata") or {}
    valid_to = _parse(meta.get("valid_to")) if meta.get("memory_form") != "event" else None
    return valid_to is not None and valid_to <= now


def _groups(results: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        g
        for g in (results or {}).get("text_mem") or []
        if isinstance(g, dict) and isinstance(g.get("memories"), list)
    ]


def _with_memories(group: dict[str, Any], memories: list[Any]) -> dict[str, Any]:
    out = {**group, "memories": memories}
    if "total_nodes" in group:
        out["total_nodes"] = len(memories)
    return out


def _rebuilt(results: dict[str, Any], per_group: list[list[Any]], top_k: int) -> dict[str, Any]:
    groups = iter(per_group)
    text_mem = [
        _with_memories(g, next(groups)[:top_k])
        if isinstance(g, dict) and isinstance(g.get("memories"), list)
        else g
        for g in results.get("text_mem") or []
    ]
    return {**results, "text_mem": text_mem}


def rerank_weight() -> float:
    """How much of relevance the time may add; 0 turns the time rerank off."""
    try:
        weight = float(os.getenv(RERANK_WEIGHT_ENV, DEFAULT_RERANK_WEIGHT))
    except ValueError:
        return DEFAULT_RERANK_WEIGHT
    return max(0.0, weight) if math.isfinite(weight) else DEFAULT_RERANK_WEIGHT


def _relevance(memory: Any, position: int) -> float:
    meta = memory.get("metadata") if isinstance(memory, dict) else None
    raw = meta.get("relativity") if isinstance(meta, dict) else None
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value):
        return max(0.0, 1.0 - _POSITION_STEP * position)
    return min(1.0, max(0.0, value))


def _recency(moments: list[datetime]) -> list[float]:
    """Newest 1, oldest 0, by rank so one very old memory does not flatten the rest."""
    distinct = sorted(set(moments))
    if len(distinct) < 2:
        return [0.0] * len(moments)
    rank = {moment: index / (len(distinct) - 1) for index, moment in enumerate(distinct)}
    return [rank[moment] for moment in moments]


def _window_fit(interval: tuple[datetime | None, datetime | None], intent: TimeIntent) -> float:
    """How much of the shorter of the two periods lies inside the other; a point inside is 1."""
    start, end = interval
    low = max(start or _FAR_PAST, intent.start)
    high = min(end or _FAR_FUTURE, intent.end)
    if high < low:
        return 0.0
    shorter = intent.end - intent.start
    if start is not None and end is not None:
        shorter = min(shorter, end - start)
    if shorter <= timedelta(0):
        return 1.0
    return min(1.0, (high - low) / shorter)


def _rerank(hits: list[_Hit], intent: TimeIntent, weight: float) -> list[_Hit]:
    """Reorder the dated, judged hits among the places they hold; every other hit stays put."""
    slots = [i for i, hit in enumerate(hits) if hit.judged and hit.moment is not None]
    if weight <= 0 or len(slots) < 2:
        return hits
    if intent.kind == "recent":
        times = _recency([hits[i].moment for i in slots])
    else:
        times = [_window_fit(hits[i].interval or (hits[i].moment, None), intent) for i in slots]
    ranked = sorted(
        (-(_relevance(hits[i].memory, i) + weight * time), i)
        for i, time in zip(slots, times, strict=True)
    )
    reranked = list(hits)
    for slot, (_, source) in zip(slots, ranked, strict=True):
        reranked[slot] = hits[source]
    return reranked


def apply_time_intent(
    results: dict[str, Any],
    intent: TimeIntent,
    *,
    top_k: int,
    rerank: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return ``results`` with ``text_mem`` read through ``intent``; the input is not mutated.

    ``recent`` and ``as_of`` also reorder the judged hits (see ``_rerank``) unless ``rerank``
    is off, e.g. because the caller asked for an explicit sort.
    """
    groups = _groups(results)
    if not groups:
        return results
    originals = [list(g["memories"]) for g in groups]
    fallback = _rebuilt(results, originals, top_k)
    weight = rerank_weight() if rerank else 0.0

    if intent.kind == "current":
        moment = now or datetime.now(UTC)
        kept = [[m for m in memories if not _expired_head(m, moment)] for memories in originals]
        return _rebuilt(results, kept, top_k) if any(kept) else fallback

    if intent.kind not in ("as_of", "recent"):
        return fallback

    def softly_recent() -> dict[str, Any]:
        reranked = [
            [hit.memory for hit in _rerank([_head_hit(m) for m in memories], intent, weight)]
            for memories in originals
        ]
        return _rebuilt(results, reranked, top_k)

    if not intent.has_window:
        return softly_recent() if intent.kind == "recent" else fallback

    per_group: list[list[Any]] = []
    matched = 0
    for memories in originals:
        hits: list[_Hit] = []
        for memory in memories:
            if not _is_judged(memory):
                hits.append(_Hit(memory))
                continue
            picked = _pick(memory, intent)
            if picked is not None:
                hits.append(picked)
                matched += 1
        per_group.append([hit.memory for hit in _rerank(hits, intent, weight)])
    if matched == 0:
        return softly_recent() if intent.kind == "recent" else fallback
    return _rebuilt(results, per_group, top_k)
