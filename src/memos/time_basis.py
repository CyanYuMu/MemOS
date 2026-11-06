"""Time basis: one vocabulary for every timestamp the service stores or returns.

The service accumulated two time semantics:

* Nodes written after the T1.3 fix carry real UTC timestamps (``utc_now_iso``).
* Nodes written before it carry *Beijing wall clock* values that Neo4j labelled
  ``+00:00`` because the string had no offset. The suffix is a lie:
  ``2026-10-21T10:00:00+00:00`` means 10:00 in Asia/Shanghai, i.e. 02:00 UTC.

The two kinds cannot be told apart from the stored value alone, so every node
carries ``metadata.time_basis``:

* ``"utc"`` - all timestamps are real UTC (every new write stamps this).
* missing / ``"legacy_local"`` - timestamps are legacy wall clock in
  ``MEMOS_LEGACY_TIME_OFFSET`` (default ``+08:00``).

Consumers never need to know which one they are looking at: the public API
normalizes every timestamp to real UTC on the way out (``memos.api``), and the
graph write path converts a legacy node in place the first time it is updated.

This module has no API or storage dependencies so both layers can share it.
"""

from __future__ import annotations

import os

from datetime import timedelta, timezone
from typing import Any

from memos.utils import parse_datetime


#: Marker written on new nodes. Bump only together with the API contract.
TIME_BASIS_UTC = "utc"
TIME_BASIS_LEGACY = "legacy_local"

#: Memory types whose timestamps were always real UTC (Dream layer).
DREAM_MEMORY_TYPES = frozenset({"DreamDiary", "InsightMemory", "Context"})

#: Every metadata field that carries an absolute time.
TIME_FIELDS = (
    "created_at",
    "updated_at",
    "chat_time",
    "valid_from",
    "valid_to",
    "event_time_start",
    "event_time_end",
    "expires_at",
    "last_dream_update",
    "archived_at",
)

#: Geometry of the graph/event projections: they use camelCase spellings.
CAMEL_TIME_FIELDS = ("createdAt", "updatedAt")

#: Legacy wall-clock offset. Beijing has had no DST since 1991, so a fixed
#: offset is exact for the data the service can hold.
DEFAULT_LEGACY_OFFSET = "+08:00"


def _parse_offset(raw: str) -> timezone:
    raw = (raw or "").strip()
    sign = 1
    if raw.startswith("-"):
        sign = -1
        raw = raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]
    hours, _, minutes = raw.partition(":")
    try:
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
    except ValueError:
        delta = timedelta(hours=8)
    return timezone(sign * delta)


def legacy_timezone() -> timezone:
    """Zone used to reinterpret legacy wall-clock values."""
    return _parse_offset(os.getenv("MEMOS_LEGACY_TIME_OFFSET", DEFAULT_LEGACY_OFFSET))


def is_legacy(metadata: dict[str, Any]) -> bool:
    """Whether ``metadata`` timestamps need the legacy reinterpretation.

    Accepts both stored metadata (``memory_type``) and the graph-node
    projection (``memoryType``, which carries ``time_basis`` at the top level).
    """
    memory_type = metadata.get("memory_type") or metadata.get("memoryType")
    if memory_type in DREAM_MEMORY_TYPES:
        return False
    return metadata.get("time_basis") != TIME_BASIS_UTC


def to_contract_time(value: Any, *, legacy: bool) -> Any:
    """Return ``value`` as an RFC 3339 UTC string.

    ``legacy`` reinterprets the wall-clock components in the legacy offset
    instead of trusting the stored ``+00:00`` suffix. Values that carry a real
    offset convert correctly either way. Unparseable values are returned
    unchanged so a malformed node cannot break a whole response.
    """
    if value is None or value == "":
        return value
    parsed = parse_datetime(value)
    if parsed is None:
        return value
    if legacy:
        shifted = parsed.replace(tzinfo=legacy_timezone())
        return shifted.astimezone(timezone.utc).isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def declares_basis(metadata: dict[str, Any]) -> bool:
    """Whether ``metadata`` states its own basis rather than inheriting one."""
    return any(key in metadata for key in ("time_basis", "memory_type", "memoryType"))


def normalize_metadata_times(metadata: dict[str, Any], *, legacy: bool | None = None) -> None:
    """Rewrite the time fields of one metadata dict in place.

    Nested structures (``history``, ``timespec``) inherit the basis of the node
    they belong to: an entry has no marker of its own, and treating it as legacy
    inside an already-UTC node would shift it by eight hours.
    """
    if legacy is None:
        legacy = is_legacy(metadata)
    for field in (*TIME_FIELDS, *CAMEL_TIME_FIELDS):
        if field in metadata:
            metadata[field] = to_contract_time(metadata[field], legacy=legacy)


def normalize_payload_times(payload: Any, *, _depth: int = 0, _legacy: bool | None = None) -> None:
    """Walk a response payload and normalize every timestamp in place.

    A dict that declares its own basis (a node's metadata, or the graph/event
    projection carrying ``memoryType``) resets the basis for its subtree; every
    other dict inherits its parent's. Unmarked content at the root is legacy by
    definition, matching ``is_legacy``.

    Skips ``embedding`` and ``sources``: embeddings are large numeric arrays and
    sources are stripped separately (or returned verbatim on request); neither
    holds timestamps the contract covers.
    """
    if _depth > 32:
        return
    if isinstance(payload, dict):
        legacy = is_legacy(payload) if declares_basis(payload) else _legacy
        if legacy is None:
            legacy = True
        normalize_metadata_times(payload, legacy=legacy)
        for key, value in payload.items():
            if key in ("embedding", "sources"):
                continue
            normalize_payload_times(value, _depth=_depth + 1, _legacy=legacy)
    elif isinstance(payload, list):
        for item in payload:
            normalize_payload_times(item, _depth=_depth + 1, _legacy=_legacy)


def stamp_utc_basis(metadata: dict[str, Any]) -> dict[str, Any]:
    """Mark freshly written metadata as real UTC.

    Callers must have produced every timestamp with ``utc_now_iso``; this only
    records the fact, it does not convert anything.
    """
    metadata["time_basis"] = TIME_BASIS_UTC
    return metadata


def convert_legacy_node(metadata: dict[str, Any], *, skip: set[str] | None = None) -> bool:
    """Convert the timestamps of a stored (raw) node to real UTC in place.

    Returns ``True`` when the node was legacy and has been converted. Nodes that
    already are UTC, or are Dream artifacts, are left untouched and return
    ``False``, so this is safe to run on every read/write.

    ``skip`` names fields the caller is about to overwrite with fresh UTC
    values; converting those would be wasted work.
    """
    if not is_legacy(metadata):
        return False
    skip = skip or set()
    for field in TIME_FIELDS:
        if field in skip or field not in metadata:
            continue
        metadata[field] = to_contract_time(metadata[field], legacy=True)
    metadata["time_basis"] = TIME_BASIS_UTC
    return True
