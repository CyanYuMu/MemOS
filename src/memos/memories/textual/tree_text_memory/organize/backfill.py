"""Backfill: fold nodes archived by the old merge flow into their survivor's ``history``.

Before the version chain, a conflict or duplicate merge archived the older node and
pointed ``MERGED_TO`` at the survivor, whose text became an LLM fusion. The older
text still sits in the archived node, but only the survivor's ``history`` can answer
"where did I live before". This copies it there.

It never deletes anything and only touches an archived node to leave one marker, so
a second run finds nothing left to do. The default is a dry run.

    python -m memos.memories.textual.tree_text_memory.organize.backfill --user-name <cube_id>
    python -m memos.memories.textual.tree_text_memory.organize.backfill --user-name <cube_id> --apply
"""

from __future__ import annotations

import argparse
import json

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from memos.log import get_logger
from memos.memories.textual.item import ArchivedTextualMemory, TextualMemoryItem
from memos.memories.textual.tree_text_memory.organize import versioning
from memos.memories.textual.tree_text_memory.organize.handler import MAX_MERGE_CHAIN_DEPTH


logger = get_logger(__name__)

FOLDED_MARKER = "history_folded_into"


@dataclass
class BackfillReport:
    user_name: str
    archived_seen: int = 0
    plan: list[tuple[str, str]] = field(default_factory=list)  # (archived id, survivor id)
    skipped: Counter = field(default_factory=Counter)
    folded: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_name": self.user_name,
            "archived_seen": self.archived_seen,
            "to_fold": len(self.plan),
            "folded": self.folded,
            "skipped": dict(self.skipped),
        }


def _survivor_id(graph, node_id: str, user_name: str) -> str | None:
    """Follow ``MERGED_TO`` to the node that is not merged further; ``None`` on a cycle or a fan-out."""
    seen = {node_id}
    current = node_id
    for _ in range(MAX_MERGE_CHAIN_DEPTH):
        targets = [
            e["to"]
            for e in graph.get_edges(
                current, type="MERGED_TO", direction="OUTGOING", user_name=user_name
            )
            if e.get("type") == "MERGED_TO" and e.get("from") == current and e.get("to")
        ]
        if not targets:
            return current if current != node_id else None
        if len(set(targets)) > 1:
            return None
        current = targets[0]
        if current in seen:
            return None
        seen.add(current)
    return None


def _read(graph, node_id: str) -> TextualMemoryItem | None:
    node = graph.get_node(node_id, include_embedding=False)
    if not node:
        return None
    try:
        return TextualMemoryItem.from_dict(node)
    except (ValueError, TypeError, KeyError):
        return None


def _is_preference(item: TextualMemoryItem) -> bool:
    meta = item.metadata
    preference = getattr(meta, "preference", None)
    return meta.memory_type == "PreferenceMemory" or (
        isinstance(preference, str) and bool(preference.strip())
    )


def _skip_reason(archived: TextualMemoryItem, survivor: TextualMemoryItem | None) -> str | None:
    if survivor is None:
        return "survivor_missing"
    if survivor.metadata.status != "activated":
        return "survivor_not_active"
    if _is_preference(archived) or _is_preference(survivor):
        return "preference"
    old, new = (archived.memory or "").strip(), (survivor.memory or "").strip()
    if not old or old == new or old in new or new in old:
        return "same_text"
    if any(
        isinstance(e, ArchivedTextualMemory) and (e.memory or "").strip() == old
        for e in survivor.metadata.history or []
    ):
        return "already_in_history"
    return None


def backfill_tenant(graph, user_name: str, *, apply: bool = False) -> BackfillReport:
    report = BackfillReport(user_name=user_name)
    archived_ids = graph.get_by_metadata([], user_name=user_name, status="archived")
    report.archived_seen = len(archived_ids)
    by_survivor: dict[str, list[TextualMemoryItem]] = {}
    for node_id in archived_ids:
        archived = _read(graph, node_id)
        if archived is None:
            report.skipped["unreadable"] += 1
            continue
        if (archived.metadata.model_extra or {}).get(FOLDED_MARKER):
            report.skipped["already_folded"] += 1
            continue
        survivor_id = _survivor_id(graph, node_id, user_name)
        if survivor_id is None:
            report.skipped["no_unique_merged_to"] += 1
            continue
        reason = _skip_reason(archived, _read(graph, survivor_id))
        if reason:
            report.skipped[reason] += 1
            continue
        report.plan.append((node_id, survivor_id))
        by_survivor.setdefault(survivor_id, []).append(archived)

    if not apply:
        return report
    for survivor_id, losers in by_survivor.items():
        survivor = _read(graph, survivor_id)
        if survivor is None:
            continue
        for loser in sorted(losers, key=versioning.version_time):
            fields = versioning.merge_into_head(survivor, loser, update_type="conflict")
            fields.pop("updated_at", None)
            graph.update_node(survivor_id, fields, user_name=user_name)
            survivor.metadata.history = [
                ArchivedTextualMemory(**json.loads(entry)) for entry in fields["history"]
            ]
            graph.update_node(loser.id, {FOLDED_MARKER: survivor_id}, user_name=user_name)
            report.folded += 1
    return report


def _build_graph():
    from memos.api.handlers.config_builders import build_graph_db_config
    from memos.graph_dbs.factory import GraphStoreFactory

    return GraphStoreFactory.from_config(build_graph_db_config())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user-name", action="append", required=True, help="cube id; repeatable")
    parser.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    args = parser.parse_args(argv)

    graph = _build_graph()
    for user_name in args.user_name:
        report = backfill_tenant(graph, user_name, apply=args.apply)
        print(json.dumps(report.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
