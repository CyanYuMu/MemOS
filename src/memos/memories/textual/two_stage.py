"""Two-stage add: coarse (immediately visible) + fine (background refinement).

Disabled by default (``TWO_STAGE_ADD=false``), in which case ``add`` behaves
exactly as before. When enabled:

1. the coarse stage writes the extracted memories as ``status="resolving"`` /
   ``stage="coarse"`` with a deterministic id derived from
   ``task_id + content_hash`` (idempotent replay);
2. the fine stage writes the normal ``activated`` nodes;
3. the coarse node is archived and linked to the fine node with ``MERGED_TO``;
4. ``rollback_coarse_nodes`` removes the coarse nodes of one task and leaves
   the fine results untouched.
"""

from __future__ import annotations

import hashlib
import os
import uuid

from typing import TYPE_CHECKING, Any

from memos.log import get_logger
from memos.memories.textual.item import TextualMemoryItem


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = get_logger(__name__)

COARSE_STATUS = "resolving"
COARSE_STAGE = "coarse"
FINE_STAGE = "fine"


def two_stage_enabled() -> bool:
    raw = os.getenv("TWO_STAGE_ADD", "false")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def coarse_node_id(task_id: str, text: str, scope: str = "") -> str:
    """Deterministic UUID for idempotent coarse writes.

    ``scope`` must identify the tenant *and* the owner: without it two users
    (or two tenants) extracting identical text under the same task id would
    map to the same UUID and overwrite each other.
    """
    key = f"{scope}:{task_id}:{content_hash(text)}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _owner_key(metadata: Any) -> str:
    owner_type = getattr(metadata, "owner_type", None)
    owner_id = getattr(metadata, "owner_id", None)
    if owner_type and owner_id:
        return f"{owner_type}:{owner_id}"
    user_id = getattr(metadata, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    agent_id = getattr(metadata, "agent_id", None)
    if agent_id:
        return f"agent:{agent_id}"
    return "unowned"


def build_coarse_items(
    memories: Iterable[TextualMemoryItem], *, task_id: str, scope: str = ""
) -> list[TextualMemoryItem]:
    """Copy the extracted memories into coarse-stage items."""
    coarse_items: list[TextualMemoryItem] = []
    for memory in memories:
        text = memory.memory or ""
        metadata = memory.metadata.model_copy(deep=True)
        metadata.status = COARSE_STATUS
        metadata.stage = COARSE_STAGE
        metadata.task_id = task_id
        metadata.content_hash = content_hash(text)
        metadata.is_fast = True
        coarse_items.append(
            TextualMemoryItem(
                id=coarse_node_id(task_id, text, f"{scope}:{_owner_key(metadata)}"),
                memory=text,
                metadata=metadata,
            )
        )
    return coarse_items


def _coarse_nodes(graph_db: Any, user_name: str | None, task_id: str) -> list[dict]:
    try:
        exported = graph_db.export_graph(
            user_name=user_name,
            filter={"task_id": task_id, "stage": COARSE_STAGE},
            page_size=None,
        )
    except Exception:
        logger.warning("Failed to query coarse nodes for task %s", task_id, exc_info=True)
        return []
    return exported.get("nodes", []) if isinstance(exported, dict) else []


def archive_coarse_nodes(
    graph_db: Any,
    *,
    user_name: str | None,
    task_id: str,
    fine_memories: Iterable[TextualMemoryItem],
) -> int:
    """Archive coarse nodes and link them to their fine counterpart."""
    if graph_db is None or not task_id:
        return 0

    fine_by_hash: dict[str, str] = {}
    for memory in fine_memories:
        fine_by_hash[content_hash(memory.memory or "")] = memory.id

    archived = 0
    for node in _coarse_nodes(graph_db, user_name, task_id):
        node_id = node.get("id")
        metadata = node.get("metadata") or {}
        if metadata.get("status") == "archived":
            continue
        fine_id = fine_by_hash.get(str(metadata.get("content_hash") or ""))
        try:
            graph_db.update_node(
                node_id, {"status": "archived", "stage": COARSE_STAGE}, user_name=user_name
            )
            if fine_id and fine_id != node_id:
                graph_db.add_edge(node_id, fine_id, "MERGED_TO", user_name=user_name)
            archived += 1
        except Exception:
            logger.warning("Failed to archive coarse node %s", node_id, exc_info=True)
    if archived:
        logger.info("Archived %d coarse nodes for task %s", archived, task_id)
    return archived


def rollback_coarse_nodes(graph_db: Any, *, user_name: str | None, task_id: str) -> int:
    """Delete the coarse nodes of one task; fine results are kept."""
    if graph_db is None or not task_id:
        return 0
    node_ids = [
        str(node.get("id"))
        for node in _coarse_nodes(graph_db, user_name, task_id)
        if node.get("id")
    ]
    if not node_ids:
        return 0
    try:
        deleted = graph_db.delete_node_by_prams(
            memory_ids=node_ids,
            writable_cube_ids=[user_name] if user_name else None,
        )
        # ``delete_node_by_prams`` also removes the Qdrant points attached to
        # the deleted ids; the per-node fallback below does not, so it is only
        # used for backends without the batch API.
        return int(deleted)
    except (AttributeError, NotImplementedError):
        pass
    except Exception:
        logger.warning("Batch rollback failed for task %s", task_id, exc_info=True)
        return 0

    deleted = 0
    for node_id in node_ids:
        try:
            graph_db.delete_node(node_id, user_name=user_name)
            deleted += 1
        except Exception:
            logger.warning("Failed to delete coarse node %s", node_id, exc_info=True)
    return deleted
