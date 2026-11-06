"""Shape responses like the MemOS cloud API: envelope, error codes, ms times, views.

The facade routes in :mod:`memos.api.cloud_routes` reuse the existing handlers and
convert their results here, so the legacy ``/product/*`` contract stays untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


#: Cloud error codes seen in probes; anything else maps to the generic 40000.
INVALID_REQUEST = 40000
MISSING_ID = 40002
LIMIT_TOO_LARGE = 40075
SERVER_ERROR = 50000

_DETAIL_TYPES = {"WorkingMemory", "LongTermMemory", "UserMemory", "OuterMemory", "RawFileMemory"}
_TYPE_TO_VIEW = {
    **dict.fromkeys(_DETAIL_TYPES, "detail_factual"),
    "EventMemory": "event",
    "PreferenceMemory": "preference",
    "ToolSchemaMemory": "tool_memory",
    "ToolTrajectoryMemory": "tool_memory",
}
_VIEW_LISTS = {
    "detail_factual": "memory_detail_list",
    "preference": "preference_detail_list",
    "event": "event_detail_list",
    "tool_memory": "tool_memory_detail_list",
    "profile": "profile_detail_list",
}


def ok(data: Any) -> dict[str, Any]:
    return {"code": 0, "data": data, "message": "ok"}


def fail(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "data": None, "message": message}


def epoch_ms(value: Any) -> int | None:
    """Cloud timestamps are epoch milliseconds; our nodes store ISO strings."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return int(value) if value > 1e11 else int(value * 1000)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _sources(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for source in metadata.get("sources") or []:
        if not isinstance(source, dict):
            continue
        out.append(
            {
                key: source[key]
                for key in ("type", "role", "chat_time", "message_id", "content", "lang")
                if source.get(key) is not None
            }
        )
    return out


def _type_extras(metadata: dict[str, Any]) -> dict[str, Any]:
    """Fields the cloud adds per memory type; only emit the ones we record.

    Preference nodes are written with ``preference``/``reasoning`` by
    ``mem_reader/read_pref_memory``; tool memories have no writer yet, so
    ``tool_type``/``tool_value`` stay out rather than being sent as nulls.
    """
    if metadata.get("memory_type") == "PreferenceMemory":
        return {
            key: metadata[key]
            for key in ("preference_type", "preference", "reasoning")
            if metadata.get(key) not in (None, "")
        }
    return {}


def to_cloud_memory(item: dict[str, Any]) -> dict[str, Any]:
    """One node / hit in the shape the cloud returns inside ``*_detail_list``."""
    metadata = item.get("metadata") or {}
    info = {
        key: metadata[key]
        for key in ("app_id", "agent_id", "user_id", "group_id")
        if metadata.get(key) is not None
    }
    return {
        "id": item.get("id"),
        "memory_key": metadata.get("key"),
        "memory_value": item.get("memory"),
        "memory_type": metadata.get("memory_type"),
        "create_time": epoch_ms(metadata.get("created_at")),
        "update_time": epoch_ms(metadata.get("updated_at")),
        "conversation_id": metadata.get("session_id"),
        "status": metadata.get("status"),
        "confidence": metadata.get("confidence"),
        "tags": metadata.get("tags") or [],
        "info": info,
        "sources": _sources(metadata),
        **_type_extras(metadata),
    }


_BUCKET_KEYS = {"text_mem", "pref_mem", "tool_mem", "profile_mem", "event_mem"}


def split_views(buckets: Any) -> dict[str, list[dict[str, Any]]]:
    """Flatten bucket lists (``{"cube_id", "memories"}``) into cloud view lists.

    Accepts a single bucket, a list of buckets, or the handler's
    ``{"text_mem": [...], ...}`` mapping.
    """
    views: dict[str, list[dict[str, Any]]] = {}
    if isinstance(buckets, dict):
        if set(buckets) & _BUCKET_KEYS:
            buckets = [item for value in buckets.values() for item in (value or [])]
        else:
            buckets = [buckets]
    for bucket in buckets or []:
        for item in (bucket or {}).get("memories") or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") or {}
            # Keep in sync with SearchHandler._is_profile_item (same detection).
            if metadata.get("memory_type") == "ProfileMemory" or item.get("template_id"):
                # ProfileService.recall already returns the cloud profile shape.
                views.setdefault(_VIEW_LISTS["profile"], []).append(item)
                continue
            view = _TYPE_TO_VIEW.get(metadata.get("memory_type"), "detail_factual")
            views.setdefault(_VIEW_LISTS[view], []).append(to_cloud_memory(item))
    return views


def view_total(buckets: Any) -> int:
    """Cloud ``total`` is the largest per-view total, not the number of returned rows."""
    if isinstance(buckets, dict):
        if set(buckets) & _BUCKET_KEYS:
            buckets = [item for value in buckets.values() for item in (value or [])]
        else:
            buckets = [buckets]
    totals = [
        int(bucket.get("total_nodes") or 0) for bucket in buckets or [] if isinstance(bucket, dict)
    ]
    return max(totals) if totals else 0


def paged(
    views: dict[str, list[dict[str, Any]]], page: int, size: int, total: int | None = None
) -> dict[str, Any]:
    if total is None:
        total = sum(len(items) for items in views.values())
    return {
        **views,
        "total": total,
        "size": size,
        "current": page,
        "pages": max(1, -(-total // size)),
    }
