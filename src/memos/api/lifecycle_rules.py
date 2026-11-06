"""Lifecycle rules for the two cloud states the open-source build lacked.

* ``expired`` — past ``expires_at`` or manually expired. Read-only and excluded
  from recall; deletion (including the cleanup job) is allowed.
* ``frozen`` — compliance lock. Rejects every update and delete until an
  operator explicitly unfreezes it outside the API.

Recall exclusion itself is enforced by the search pipeline: only ``activated``
memories (plus ``resolving`` when two-stage add is on) are returned.
"""

from __future__ import annotations

from typing import Any

from memos.log import get_logger


logger = get_logger(__name__)

LIFECYCLE_ACTIVATED = "activated"
LIFECYCLE_ARCHIVED = "archived"
LIFECYCLE_EXPIRED = "expired"
LIFECYCLE_FROZEN = "frozen"

WRITABLE_STATUSES = {LIFECYCLE_ACTIVATED, LIFECYCLE_ARCHIVED}
KNOWN_STATUSES = {
    LIFECYCLE_ACTIVATED,
    LIFECYCLE_ARCHIVED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_FROZEN,
    "resolving",
    "deleted",
}

#: Transitions an API caller may request. Frozen/expired nodes are terminal
#: from the API's point of view (an operator can still change them directly).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    LIFECYCLE_ACTIVATED: {LIFECYCLE_ARCHIVED, LIFECYCLE_EXPIRED, LIFECYCLE_FROZEN},
    LIFECYCLE_ARCHIVED: {LIFECYCLE_ACTIVATED, LIFECYCLE_EXPIRED, LIFECYCLE_FROZEN},
}


def lifecycle_guard(
    current_status: str | None,
    action: str,
    target_status: str | None = None,
) -> str | None:
    """Return a human readable reason when ``action`` must be rejected."""
    status = current_status or LIFECYCLE_ACTIVATED

    if status == LIFECYCLE_FROZEN:
        return (
            "memory is frozen (compliance lock): updates and deletes are rejected; "
            "an operator must unfreeze it first"
        )

    if action == "update" and status == LIFECYCLE_EXPIRED:
        return "memory is expired and read-only"

    if action == "update" and target_status is not None:
        if target_status not in KNOWN_STATUSES:
            return f"unsupported target status: {target_status}"
        if target_status != status and target_status not in ALLOWED_TRANSITIONS.get(status, set()):
            return f"status transition {status} -> {target_status} is not allowed"

    return None


def expire_memories(
    graph_db: Any,
    *,
    user_name: str | None = None,
    now: str | None = None,
) -> list[str]:
    """Mark memories whose ``expires_at`` has passed as ``expired``.

    Idempotent: expired and frozen nodes are never touched again, so a second
    run returns an empty list.
    """
    from memos.utils import utc_now_iso

    cutoff = now or utc_now_iso()
    try:
        exported = graph_db.export_graph(
            user_name=user_name,
            status=[LIFECYCLE_ACTIVATED, LIFECYCLE_ARCHIVED],
            filter={"expires_at": {"lte": cutoff}},
            page_size=None,
        )
    except Exception:
        logger.warning("Failed to query expired memories", exc_info=True)
        return []

    expired_ids: list[str] = []
    for node in exported.get("nodes", []) if isinstance(exported, dict) else []:
        memory_id = node.get("id")
        if not memory_id:
            continue
        try:
            graph_db.update_node(
                memory_id,
                {"status": LIFECYCLE_EXPIRED, "updated_at": utc_now_iso()},
                user_name=user_name,
            )
            expired_ids.append(memory_id)
        except Exception:
            logger.warning("Failed to expire memory %s", memory_id, exc_info=True)

    if expired_ids:
        logger.info("Marked %d memories as expired", len(expired_ids))
    return expired_ids
