"""Keep one live ``ToolSchemaMemory`` per tool name and user.

Schema ids hash the schema, so a tool whose definition changed lands on a new node while the
old one stays activated and keeps being recalled. ``skip_live_schemas`` drops a schema that is
already stored, so a request repeating it rewrites nothing (a rewrite would reset the node's
``history``). After the new nodes are written, ``fold_replaced_schemas`` archives the replaced
ones and turns their definitions into history of the new one. Deterministic: no LLM.
"""

from __future__ import annotations

import json

from typing import Any

from memos.log import get_logger
from memos.memories.textual.item import ArchivedTextualMemory, TextualMemoryItem
from memos.memories.textual.tree_text_memory.organize import versioning


logger = get_logger(__name__)

TOOL_SCHEMA_TYPE = "ToolSchemaMemory"


def tool_name(memory: str | None) -> str | None:
    """Name of the tool a stored schema describes, or ``None`` if the text is not a schema."""
    try:
        schema = json.loads(memory or "")
    except ValueError:
        return None
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    source = function if isinstance(function, dict) else schema
    name = source.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def skip_live_schemas(
    graph: Any, memories: list[TextualMemoryItem], user_name: str | None = None
) -> list[TextualMemoryItem]:
    """``memories`` without the schemas that are already stored and activated.

    The id hashes the schema, so the same id means the same definition.
    """
    kept = []
    for item in memories:
        if item.metadata.memory_type == TOOL_SCHEMA_TYPE:
            try:
                stored = graph.get_node(item.id, user_name=user_name)
            except Exception as e:  # noqa: BLE001 - when in doubt, write it
                logger.warning("[TOOL_SCHEMA] could not look up %s: %s", item.id, e)
                stored = None
            if stored and (stored.get("metadata") or {}).get("status", "activated") == "activated":
                continue
        kept.append(item)
    return kept


def _live_schemas(graph: Any, user_id: str, user_name: str | None) -> list[TextualMemoryItem]:
    filters = [
        {"field": "memory_type", "op": "=", "value": TOOL_SCHEMA_TYPE},
        {"field": "user_id", "op": "=", "value": user_id},
    ]
    ids = graph.get_by_metadata(filters, user_name=user_name, status="activated")
    if not ids:
        return []
    live = []
    for node in graph.get_nodes(list(ids), user_name=user_name):
        try:
            live.append(TextualMemoryItem.from_dict(node))
        except (ValueError, TypeError, KeyError):
            continue
    return live


def fold_replaced_schemas(
    graph: Any, written: list[TextualMemoryItem], user_name: str | None = None
) -> int:
    """Archive the older schema of every tool that ``written`` redefines; returns how many.

    Call it after the new nodes are stored, so a failed write never leaves a tool without a
    live schema. Never raises: the write itself already succeeded.
    """
    folded = 0
    heads: dict[tuple[str, str], TextualMemoryItem] = {}
    for item in written:
        name = tool_name(item.memory)
        if item.metadata.memory_type == TOOL_SCHEMA_TYPE and name and item.metadata.user_id:
            heads[(item.metadata.user_id, name)] = item
    written_ids = {item.id for item in written}

    for (user_id, name), head in heads.items():
        try:
            # The graph writers swallow their own errors, so confirm the new schema is stored
            # before retiring the old one.
            if not graph.get_node(head.id, user_name=user_name):
                continue
            replaced = [
                old
                for old in _live_schemas(graph, user_id, user_name)
                if old.id not in written_ids and tool_name(old.memory) == name
            ]
            for old in sorted(replaced, key=versioning.version_time):
                fields = versioning.merge_into_head(head, old, update_type="update")
                fields.pop("updated_at", None)
                graph.update_node(head.id, fields, user_name=user_name)
                head.metadata.history = [
                    ArchivedTextualMemory(**json.loads(entry)) for entry in fields["history"]
                ]
                graph.update_node(
                    old.id,
                    {"status": "archived", "valid_to": fields["valid_from"]},
                    user_name=user_name,
                )
                graph.add_edge(old.id, head.id, "MERGED_TO", user_name=user_name)
                folded += 1
        except Exception as e:  # noqa: BLE001 - the schema is stored; versioning is best effort
            logger.warning("[TOOL_SCHEMA] could not archive older schemas of %s: %s", name, e)
    return folded
