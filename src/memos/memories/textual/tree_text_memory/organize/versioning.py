"""Version chain for state memories.

A node always holds the *current* version of a memory. Earlier versions live in
its ``history`` (``ArchivedTextualMemory`` entries) together with the period each
one was valid, so "where did I live before" and "where did I live in June 2025"
can be answered from the head node without a second vector per version.

Everything here is deterministic. Which side is newer is decided from time
stamps, never by an LLM; the LLM only judges whether two statements are about
the same changeable fact (see ``NodeHandler.detect``).
"""

from __future__ import annotations

import json

from datetime import datetime, timezone
from typing import Any

from memos.memories.textual.item import ArchivedTextualMemory, TextualMemoryItem
from memos.time_basis import is_legacy, to_contract_time
from memos.utils import normalize_to_utc_iso, parse_datetime, utc_now_iso


# Hot keys (a status that changes every week) must not grow a node without bound.
MAX_HISTORY_VERSIONS = 50

_TIMESPEC_KEYS = ("valid_from", "valid_to", "chat_time", "event_time_start", "event_time_end")


def memory_form(item: Any) -> str:
    """``state`` unless the node is explicitly an ``event`` (legacy nodes are states)."""
    form = getattr(getattr(item, "metadata", None), "memory_form", None)
    return "event" if form == "event" else "state"


def is_event(item: Any) -> bool:
    return memory_form(item) == "event"


def _dt(value: Any) -> datetime | None:
    iso = normalize_to_utc_iso(value)
    return parse_datetime(iso) if iso else None


def _meta(item: Any, name: str) -> Any:
    return getattr(getattr(item, "metadata", None), name, None)


def _utc_meta(item: Any, name: str) -> Any:
    """``metadata.<name>`` as real UTC, read in the node's own basis.

    Snapshotting a pre-T1.3 node must not copy its wall-clock value into a node
    the write path is about to mark as UTC; comparing a legacy value with a UTC
    one would also be wrong by eight hours.
    """
    value = _meta(item, name)
    if value is None:
        return None
    metadata = getattr(item, "metadata", None)
    basis = {
        "time_basis": getattr(metadata, "time_basis", None),
        "memory_type": getattr(metadata, "memory_type", None),
    }
    return to_contract_time(value, legacy=is_legacy(basis))


def order_pair(incoming: Any, existing: Any) -> tuple[Any, Any]:
    """Return ``(older, newer)`` of two nodes describing the same fact.

    ``chat_time`` (when the user said it) decides when both nodes have one, so a
    back-filled old conversation cannot overwrite a newer fact. Otherwise the
    write order decides. ``created_at`` and ``chat_time`` are never compared with
    each other: ``created_at`` is when the row was written, which says nothing
    about when the fact changed. Both sides are read in their own time basis, so
    a pre-T1.3 node still compares correctly. A full tie means the incoming node
    is the newer one.
    """
    for name in ("chat_time", "created_at"):
        t_in, t_ex = _dt(_utc_meta(incoming, name)), _dt(_utc_meta(existing, name))
        if t_in is not None and t_ex is not None and t_in != t_ex:
            return (incoming, existing) if t_in < t_ex else (existing, incoming)
    return existing, incoming


def version_time(item: Any) -> str:
    """When this version of the memory became true (ISO, UTC)."""
    for name in ("valid_from", "chat_time", "created_at", "updated_at"):
        iso = normalize_to_utc_iso(_utc_meta(item, name))
        if iso:
            return iso
    return utc_now_iso()


def display_time(item: Any) -> str:
    """Time stamp shown to the LLM that judges a pair; ``unknown`` without a ``chat_time``.

    ``created_at`` says when the row was written, not when the fact changed, so it is not shown
    at all; the write order still decides direction (see ``order_pair``).
    """
    return normalize_to_utc_iso(_utc_meta(item, "chat_time")) or "unknown"


def _timespec(item: Any, *, valid_to: str | None) -> dict[str, str]:
    spec = {
        "valid_from": version_time(item),
        "valid_to": (valid_to if valid_to is not None else _utc_meta(item, "valid_to")),
        "chat_time": _utc_meta(item, "chat_time"),
        "event_time_start": _utc_meta(item, "event_time_start"),
        "event_time_end": _utc_meta(item, "event_time_end"),
    }
    return {k: v for k, v in spec.items() if k in _TIMESPEC_KEYS and v}


def history_entry(item: Any, *, update_type: str, valid_to: str | None) -> dict[str, Any]:
    """Snapshot ``item``'s current text as a history entry (version is set by the caller)."""
    entry: dict[str, Any] = {
        "memory": item.memory,
        "update_type": update_type,
        "created_at": _utc_meta(item, "created_at") or utc_now_iso(),
        "memory_form": memory_form(item),
        "timespec": _timespec(item, valid_to=valid_to),
    }
    return entry


def _existing_entries(item: Any) -> list[dict[str, Any]]:
    out = []
    for entry in _meta(item, "history") or []:
        if isinstance(entry, ArchivedTextualMemory):
            entry = entry.model_dump(exclude_none=True)
        if isinstance(entry, dict) and entry.get("memory"):
            out.append(dict(entry))
    return out


def _entry_time(entry: dict[str, Any]) -> datetime:
    spec = entry.get("timespec") or {}
    parsed = _dt(spec.get("valid_from")) or _dt(entry.get("created_at"))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def build_history(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological, de-duplicated, renumbered from 1, capped to the newest versions."""
    seen: set[tuple[str, str]] = set()
    unique = []
    for entry in sorted(entries, key=_entry_time):
        key = (entry["memory"], (entry.get("timespec") or {}).get("valid_from", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    unique = unique[-MAX_HISTORY_VERSIONS:]
    for index, entry in enumerate(unique, start=1):
        entry["version"] = index
    return unique


def serialize_history(entries: list[dict[str, Any]]) -> list[str]:
    """The shape graph stores keep: one JSON string per entry (maps are not Neo4j properties)."""
    return [json.dumps(entry, ensure_ascii=False) for entry in entries]


def merge_into_head(head: Any, tail: Any, *, update_type: str = "conflict") -> dict[str, Any]:
    """Fold ``tail`` (the older version) into ``head`` (the current one).

    Returns the node fields to write on ``head``. Its text is untouched; ``tail``'s
    text and its own earlier versions become history, and the version that was
    replaced is closed at the moment ``head`` became valid.
    """
    head_from = version_time(head)
    entries = _existing_entries(head) + _existing_entries(tail)
    entries.append(history_entry(tail, update_type=update_type, valid_to=head_from))
    history = build_history(entries)
    return {
        "history": serialize_history(history),
        "version": len(history) + 1,
        "valid_from": head_from,
        "memory_form": memory_form(head),
        "updated_at": utc_now_iso(),
    }


def apply_feedback_update(
    old_item: TextualMemoryItem, new_item: TextualMemoryItem, user_name: str | None = None
) -> tuple[TextualMemoryItem, None, None, dict[str, Any]]:
    """``memory_version.apply_feedback_update`` built-in: update ``old_item`` in place.

    The old text is kept in ``history``; no archived node is created, so the
    node keeps its id, its edges and its place in the tree. Returns
    ``(updated_item, archived_item, archived_metadata, updated_fields)`` as the
    hook contract requires; the two archived values are always ``None``.
    ``updated_fields`` only holds Neo4j-safe values (history and sources as JSON
    strings).
    """
    now = utc_now_iso()
    new_from = normalize_to_utc_iso(_meta(new_item, "chat_time")) or now
    entries = _existing_entries(old_item)
    entries.append(history_entry(old_item, update_type="feedback", valid_to=new_from))
    history = build_history(entries)

    updated = old_item.model_copy(deep=True)
    meta = updated.metadata
    updated.memory = new_item.memory
    meta.key = new_item.metadata.key
    meta.tags = new_item.metadata.tags
    meta.background = new_item.metadata.background
    if getattr(new_item.metadata, "sources", None) is not None:
        meta.sources = list(new_item.metadata.sources or []) + list(meta.sources or [])
    if getattr(new_item.metadata, "embedding", None) is not None:
        meta.embedding = new_item.metadata.embedding
    if meta.memory_type == "PreferenceMemory":
        meta.preference = updated.memory
    meta.version = len(history) + 1
    meta.valid_from = new_from
    meta.valid_to = None
    meta.updated_at = now
    meta.history = [ArchivedTextualMemory(**entry) for entry in history]

    fields: dict[str, Any] = {
        "memory": updated.memory,
        "key": meta.key,
        "tags": meta.tags,
        "background": meta.background,
        # 这里只交出原始结构，序列化统一由 graph_db.update_node 做一次，
        # 否则同一次更新会被 dumps 两遍（历史 bug：sources 被套娃放大）。
        "sources": [
            source.model_dump(exclude_none=True) if hasattr(source, "model_dump") else source
            for source in (meta.sources or [])
        ],
        "history": serialize_history(history),
        "version": meta.version,
        "valid_from": new_from,
        "memory_form": memory_form(old_item),
        "updated_at": now,
    }
    if meta.embedding is not None:
        fields["embedding"] = meta.embedding
    if meta.memory_type == "PreferenceMemory":
        fields["preference"] = meta.preference
    return updated, None, None, fields
