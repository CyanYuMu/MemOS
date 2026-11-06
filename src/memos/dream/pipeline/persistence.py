from __future__ import annotations

import logging

from typing import Any
from uuid import uuid4

from memos.dream.embedding import embed_text
from memos.dream.hook_defs import DreamH
from memos.dream.types import DreamActionType, TargetMemoryType
from memos.plugins.hooks import trigger_hook
from memos.utils import utc_now_iso


logger = logging.getLogger(__name__)


class DreamPersistence:
    """Dream persistence strategy.

    Two-track write:
      1. Memory store write-back — execute each DreamAction against the
         appropriate memory type (LongTermMemory, etc.)
      2. Dream Diary write — store the diary entry in graph_db for retrieval.

    Persistence conditions (enforced before writing):
      - Hypothetical deduction: the action must carry a non-empty `rationale`
        demonstrating that a question can be answered better with this memory.
      - Zero-confidence actions are skipped.
    """

    def __init__(self) -> None:
        self.context: dict[str, Any] = {}

    def bind_context(self, context: dict) -> None:
        self.context = context

    def persist(
        self,
        *,
        results,
        text_mem,
        cube_id: str,
        mem_cube_id: str,
        user_id: str,
        signal_snapshot,
    ) -> None:
        trigger_hook(DreamH.DREAM_BEFORE_PERSIST, mem_cube_id=mem_cube_id, results=results)

        for result in results:
            self._execute_actions(
                actions=result.actions,
                text_mem=text_mem,
                cube_id=cube_id,
                mem_cube_id=mem_cube_id,
                user_id=user_id,
            )
            self._persist_diary(
                result=result,
                cube_id=cube_id,
                mem_cube_id=mem_cube_id,
                user_id=user_id,
                signal_snapshot=signal_snapshot,
            )

        trigger_hook(DreamH.DREAM_AFTER_PERSIST, mem_cube_id=mem_cube_id, results=results)

    def _execute_actions(
        self,
        *,
        actions,
        text_mem,
        cube_id: str,
        mem_cube_id: str,
        user_id: str,
    ) -> None:
        """Execute DreamActions against the memory store.

        Only actions that pass persistence conditions are written:
        - Must have a non-empty rationale (hypothetical deduction)
        - Must have confidence > 0
        """
        if text_mem is None:
            logger.info("[Dream] text_mem is unavailable; skip memory write-back.")
            return

        for action in actions:
            if not self._passes_persistence_condition(action):
                logger.debug(
                    "[Dream] action skipped (condition not met): type=%s, target=%s",
                    action.action_type.value,
                    action.target_memory_type.value,
                )
                continue

            try:
                self._dispatch_action(
                    action=action,
                    text_mem=text_mem,
                    cube_id=cube_id,
                    mem_cube_id=mem_cube_id,
                    user_id=user_id,
                )
            except Exception:
                logger.exception(
                    "[Dream] failed to execute action: type=%s, target_memory_type=%s",
                    action.action_type.value,
                    action.target_memory_type.value,
                )

    @staticmethod
    def _passes_persistence_condition(action) -> bool:
        """Enforce persistence conditions.

        A Dream action must justify its existence: the rationale field must
        explain how this memory helps answer a concrete question better.
        """
        if not action.rationale:
            return False
        return not action.confidence <= 0

    def _dispatch_action(
        self,
        *,
        action,
        text_mem,
        cube_id: str,
        mem_cube_id: str,
        user_id: str,
    ) -> None:
        """Route a DreamAction to the appropriate memory write operation.

        Community implementations should flesh out each branch with the real
        memory-type-specific write logic.
        """
        dream_metadata = {
            "source": "dream",
            "dream_action_type": action.action_type.value,
            "target_memory_type": action.target_memory_type.value,
            "rationale": action.rationale,
            "confidence": action.confidence,
            "source_memory_ids": action.source_memory_ids,
        }

        if action.target_memory_type == TargetMemoryType.DREAM_DIARY:
            return

        if action.action_type == DreamActionType.CREATE:
            self._write_memory_create(
                action=action,
                text_mem=text_mem,
                cube_id=cube_id,
                dream_metadata=dream_metadata,
            )
        elif action.action_type == DreamActionType.UPDATE:
            self._write_memory_update(
                action=action,
                text_mem=text_mem,
                cube_id=cube_id,
                dream_metadata=dream_metadata,
            )
        elif action.action_type == DreamActionType.MERGE:
            self._write_memory_merge(
                action=action,
                text_mem=text_mem,
                cube_id=cube_id,
                dream_metadata=dream_metadata,
            )
        elif action.action_type == DreamActionType.ARCHIVE:
            self._write_memory_archive(
                action=action,
                text_mem=text_mem,
                cube_id=cube_id,
                dream_metadata=dream_metadata,
            )

    def _write_memory_create(self, *, action, text_mem, cube_id, dream_metadata) -> None:
        """Create a new memory node from Dream reasoning output."""
        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None:
            logger.info("[Dream] graph_db unavailable for CREATE action.")
            return

        # The vector DB rejects any id that is not a UUID, so the id must not
        # carry a readable prefix.
        node_id = str(uuid4())
        metadata = {
            "type": action.target_memory_type.value,
            "memory_type": action.target_memory_type.value,
            "status": "activated",
            "source": "dream",
            "created_at": utc_now_iso(),
            **dream_metadata,
            **action.metadata,
            "embedding": embed_text(self.context, action.new_content),
        }
        graph_db.add_node(node_id, action.new_content, metadata, user_name=cube_id)
        logger.info(
            "[Dream] created memory node: id=%s, type=%s",
            node_id,
            action.target_memory_type.value,
        )

    def _write_memory_update(self, *, action, text_mem, cube_id, dream_metadata) -> None:
        """Update an existing memory node with Dream-consolidated content."""
        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None or not action.target_memory_id:
            return

        fields = {
            "updated_at": utc_now_iso(),
            "last_dream_update": utc_now_iso(),
            **dream_metadata,
            **action.metadata,
        }
        # An UPDATE that carries no new text must not blank out the memory.
        if action.new_content:
            fields["memory"] = action.new_content
        try:
            graph_db.update_node(
                action.target_memory_id,
                fields,
                user_name=cube_id,
            )
            logger.info("[Dream] updated memory: id=%s", action.target_memory_id)
        except Exception:
            logger.exception("[Dream] update_node failed: id=%s", action.target_memory_id)

    def _write_memory_merge(self, *, action, text_mem, cube_id, dream_metadata) -> None:
        """Merge multiple source memories into a new consolidated node."""
        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None:
            return

        # The vector DB rejects any id that is not a UUID, so the id must not
        # carry a readable prefix.
        merged_id = str(uuid4())
        metadata = {
            "type": action.target_memory_type.value,
            "memory_type": action.target_memory_type.value,
            "status": "activated",
            "source": "dream",
            "merged_from": action.source_memory_ids,
            "created_at": utc_now_iso(),
            **dream_metadata,
            **action.metadata,
            "embedding": embed_text(self.context, action.new_content),
        }
        graph_db.add_node(merged_id, action.new_content, metadata, user_name=cube_id)

        for src_id in action.source_memory_ids:
            try:
                graph_db.update_node(
                    src_id,
                    {"status": "archived", "merged_into": merged_id},
                    user_name=cube_id,
                )
            except Exception:
                logger.warning("[Dream] could not archive merged source %s", src_id)

        logger.info("[Dream] merged %d memories → %s", len(action.source_memory_ids), merged_id)

    def _write_memory_archive(self, *, action, text_mem, cube_id, dream_metadata) -> None:
        """Archive a memory that Dream deems no longer valuable."""
        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None or not action.target_memory_id:
            return

        try:
            graph_db.update_node(
                action.target_memory_id,
                {
                    "status": "archived",
                    "archived_at": utc_now_iso(),
                    "archive_reason": action.rationale,
                    **dream_metadata,
                },
                user_name=cube_id,
            )
            logger.info("[Dream] archived memory: id=%s", action.target_memory_id)
        except Exception:
            logger.exception("[Dream] archive failed: id=%s", action.target_memory_id)

    def _persist_diary(
        self,
        *,
        result,
        cube_id: str,
        mem_cube_id: str,
        user_id: str,
        signal_snapshot,
    ) -> None:
        """Persist the Dream diary entry to graph_db."""
        entry = result.diary_entry
        if entry is None:
            return

        graph_db = self.context.get("shared", {}).get("graph_db")
        if graph_db is None:
            logger.info("[Dream] graph_db unavailable; skip diary persistence.")
            return

        content = entry.format_content()
        metadata = self._build_diary_metadata(
            entry=entry,
            result=result,
            user_id=user_id,
            mem_cube_id=mem_cube_id,
            signal_snapshot=signal_snapshot,
        )
        metadata["embedding"] = embed_text(self.context, content)
        try:
            graph_db.add_node(
                entry.diary_id,
                content,
                metadata,
                user_name=cube_id,
            )
        except Exception:
            logger.exception(
                "[Dream] failed to persist diary: mem_cube_id=%s, cluster=%s",
                mem_cube_id,
                result.cluster_id,
            )

    @staticmethod
    def _build_diary_metadata(*, entry, result, user_id, mem_cube_id, signal_snapshot) -> dict:
        return {
            "type": "dream_diary",
            "memory_type": "DreamDiary",
            "status": entry.status,
            "source": "system",
            # The node id has to be a bare UUID for the vector DB, so the
            # readable id the diary API reports is kept here instead.
            "task_id": entry.task_id,
            "title": entry.title,
            "summary": entry.summary,
            "dream_entry": entry.dream_entry,
            "motive": entry.motive,
            "context_events": entry.context_events,
            "themes": entry.themes,
            "tags": ["dream", "diary"],
            "created_at": entry.created_at.isoformat(),
            "updated_at": utc_now_iso(),
            "user_id": user_id,
            "session_id": getattr(signal_snapshot, "session_id", ""),
            "actions_summary": [
                {
                    "type": a.action_type.value,
                    "target": a.target_memory_type.value,
                    "rationale": a.rationale,
                }
                for a in result.actions
            ],
            "info": {
                "cluster_id": result.cluster_id,
                "mem_cube_id": mem_cube_id,
                "pending_memory_ids": list(getattr(signal_snapshot, "pending_memory_ids", [])),
            },
        }
