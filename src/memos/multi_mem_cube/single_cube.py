from __future__ import annotations

import json
import time
import traceback

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memos.api.handlers.formatters_handler import (
    format_memory_item,
    post_process_textual_mem,
)
from memos.log import (
    get_logger,
    summarize_add_request,
    summarize_search_request,
    summarize_search_results,
)
from memos.mem_reader.tag_matcher import apply_semantic_custom_tags
from memos.mem_scheduler.schemas.message_schemas import ScheduleMessageItem
from memos.mem_scheduler.schemas.task_schemas import (
    ADD_TASK_LABEL,
    MEM_FEEDBACK_TASK_LABEL,
    MEM_READ_TASK_LABEL,
)
from memos.memories.textual.item import TextualMemoryItem
from memos.memories.textual.owner import (
    OwnerView,
    build_write_views,
    filter_by_allow_memory_view,
    normalize_id_list,
    stamp_owner,
)
from memos.memories.textual.two_stage import (
    archive_coarse_nodes,
    build_coarse_items,
    two_stage_enabled,
)
from memos.multi_mem_cube.views import MemCubeView
from memos.search import resolve_filter_for_cube, search_text_memories
from memos.types.general_types import (
    FINE_STRATEGY,
    FineStrategy,
    MOSSearchResult,
    SearchMode,
    UserContext,
)
from memos.utils import normalize_to_utc_iso, timed, timed_stage, utc_now


logger = get_logger(__name__)

# handler 在去重模式下把 top_k 乘这个倍数传给召回。
_GAP_RECALL_DEDUP_FACTOR = 3


def _includes_embedding(dedup: str | None) -> bool:
    """MMR / sim 去重需要向量；不带的话去重阶段会重算全部候选的 embedding。"""
    return dedup in ("sim", "mmr")


def _as_memory_ids(values: Any) -> list[str]:
    """把 text-memory ``add()`` 的返回值统一成 id 字符串（契约是 ``list[str]``）。"""
    ids: list[str] = []
    for value in values or []:
        if isinstance(value, str):
            ids.append(value)
        elif isinstance(value, dict):
            ids.append(str(value.get("id", "")))
        else:
            ids.append(str(getattr(value, "id", value)))
    return ids


if TYPE_CHECKING:
    from memos.api.product_models import APIADDRequest, APIFeedbackRequest, APISearchRequest
    from memos.mem_cube.navie import NaiveMemCube
    from memos.mem_reader.simple_struct import SimpleStructMemReader
    from memos.mem_scheduler.optimized_scheduler import OptimizedScheduler


@dataclass
class SingleCubeView(MemCubeView):
    cube_id: str
    naive_mem_cube: NaiveMemCube
    mem_reader: SimpleStructMemReader
    mem_scheduler: OptimizedScheduler
    logger: Any
    searcher: Any
    feedback_server: Any | None = None
    deepsearch_agent: Any | None = None

    @timed
    def add_memories(self, add_req: APIADDRequest) -> list[dict[str, Any]]:
        """
        This is basically your current handle_add_memories logic,
        but scoped to a single cube_id.

        One extraction view is produced per owner: a plain single-user request
        keeps exactly one view (historical behavior and cost); group chat,
        agent-independent memory and public memory add views on top.
        """
        sync_mode = add_req.async_mode or self._get_sync_mode()
        self.logger.info(
            f"[DIAGNOSTIC] single_cube.add_memories called for cube_id: {self.cube_id}. "
            f"sync_mode: {sync_mode}. request={summarize_add_request(add_req)}"
        )

        raw_messages = getattr(add_req, "raw_messages", None)
        views = build_write_views(
            user_id=add_req.user_id,
            agent_id=getattr(add_req, "agent_id", None),
            group_id=getattr(add_req, "group_id", None),
            messages=(
                raw_messages
                if raw_messages is not None
                else (add_req.messages if isinstance(add_req.messages, list) else None)
            ),
            info=add_req.info,
            allow_public=bool(getattr(add_req, "allow_public", False)),
            tenant=self.cube_id,
        )

        all_memories: list[dict[str, Any]] = []
        for view in views:
            # Always narrow the request to a single string user_id: a list of one
            # participant would otherwise leak into UserContext / scheduler.
            view_req = self._request_for_view(add_req, view)
            user_context = UserContext(
                user_id=view.user_id or view.agent_id or self.cube_id,
                mem_cube_id=self.cube_id,
                session_id=add_req.session_id or "default_session",
                manager_user_id=add_req.manager_user_id,
                project_id=add_req.project_id,
            )
            all_memories.extend(
                self._process_text_mem(view_req, user_context, sync_mode, owner_view=view)
            )

        self.logger.info(f"[SingleCubeView] cube={self.cube_id} total_results={len(all_memories)}")
        return all_memories

    @staticmethod
    def _request_for_view(add_req: APIADDRequest, view: OwnerView) -> APIADDRequest:
        """Narrow the request to one owner view (messages / ids / info)."""
        info = dict(view.info)
        if view.agent_id:
            info.setdefault("agent_id", view.agent_id)
        if view.group_id:
            info.setdefault("group_id", view.group_id)

        user_ids = normalize_id_list(add_req.user_id)
        return add_req.model_copy(
            update={
                "user_id": view.user_id or (user_ids[0] if user_ids else view.agent_id),
                "agent_id": view.agent_id,
                "group_id": view.group_id,
                "allow_public": False,
                "messages": view.messages,
                "raw_messages": view.messages,
                "info": info,
            }
        )

    @timed
    def search_memories(self, search_req: APISearchRequest) -> dict[str, Any]:
        """
        Unified memory search handling (text + preference memories).
        Preference memories are now searched through the same _search_text flow.
        """
        cube_filter = resolve_filter_for_cube(search_req.filter, self.cube_id)
        cube_filter = self._with_owner_scope(search_req, cube_filter)
        if cube_filter is not search_req.filter:
            import copy

            search_req = copy.copy(search_req)
            search_req.filter = cube_filter

        # Create UserContext object
        user_context = UserContext(
            user_id=search_req.user_id,
            mem_cube_id=self.cube_id,
            session_id=search_req.session_id or "default_session",
        )
        self.logger.info("Search request summary: %s", summarize_search_request(search_req))

        memories_result: MOSSearchResult = {
            "text_mem": [],
            "act_mem": [],
            "para_mem": [],
            "pref_mem": [],
            "dream_mem": [],
            "pref_note": "",
            "tool_mem": [],
            "event_mem": [],
            "profile_mem": [],
        }

        # Determine search mode
        search_mode = self._get_search_mode(search_req.mode)

        # Unified search through _search_text (includes all memory types)
        all_formatted_memories = self._search_text(search_req, user_context, search_mode)

        # Build result with unified processing
        memories_result = post_process_textual_mem(
            memories_result,
            all_formatted_memories,
            self.cube_id,
        )

        self.logger.info("Search result summary: %s", summarize_search_results(memories_result))
        return memories_result

    def _with_owner_scope(
        self, search_req: APISearchRequest, cube_filter: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Constrain the read to the viewer's owner scopes.

        Legacy nodes (written before the owner migration) are still returned
        through the ``owner_type IS NULL`` branch, matched on their
        compatibility ``user_id`` / ``agent_id`` property, so enabling the
        guard before the backfill does not silently lose memories.
        """
        from memos.memories.textual.owner import (
            inject_owner_scope,
            normalize_id_list,
            owner_scope_enforced,
            owner_scope_filter,
        )

        if not owner_scope_enforced():
            return cube_filter

        user_ids = (
            [search_req.user_id.strip()]
            if search_req.user_id and search_req.user_id.strip()
            else []
        )
        agent_ids = normalize_id_list(getattr(search_req, "agent_id", None))
        group_id = getattr(search_req, "group_id", None)
        group_ids = [group_id] if group_id else []
        if not user_ids and not agent_ids:
            # Fail closed: a read without any subject used to fall back to an
            # unscoped tenant-wide query and could return every participant's
            # private / agent / group memory.
            return inject_owner_scope(cube_filter, {"owner_type": "__none__"})

        scope = owner_scope_filter(
            user_ids=user_ids,
            agent_ids=agent_ids,
            group_ids=group_ids,
            tenant=self.cube_id,
        )
        return inject_owner_scope(cube_filter, scope)

    @timed
    def feedback_memories(self, feedback_req: APIFeedbackRequest) -> dict[str, Any]:
        target_session_id = feedback_req.session_id or "default_session"
        if feedback_req.async_mode == "async":
            try:
                feedback_req_str = json.dumps(feedback_req.model_dump())
                message_item_feedback = ScheduleMessageItem(
                    user_id=feedback_req.user_id,
                    task_id=feedback_req.task_id,
                    session_id=target_session_id,
                    mem_cube_id=self.cube_id,
                    mem_cube=self.naive_mem_cube,
                    label=MEM_FEEDBACK_TASK_LABEL,
                    content=feedback_req_str,
                    timestamp=utc_now(),
                )
                # Use scheduler submission to ensure tracking and metrics
                self.mem_scheduler.submit_messages(messages=[message_item_feedback])
                self.logger.info(f"[SingleCubeView] cube={self.cube_id} Submitted FEEDBACK async")
            except Exception as e:
                self.logger.error(
                    f"[SingleCubeView] cube={self.cube_id} Failed to submit FEEDBACK: {e}",
                    exc_info=True,
                )
            return []
        else:
            feedback_result = self.feedback_server.process_feedback(
                user_id=feedback_req.user_id,
                user_name=self.cube_id,
                session_id=feedback_req.session_id,
                chat_history=feedback_req.history,
                retrieved_memory_ids=feedback_req.retrieved_memory_ids,
                feedback_content=feedback_req.feedback_content,
                feedback_time=feedback_req.feedback_time,
                async_mode=feedback_req.async_mode,
                corrected_answer=feedback_req.corrected_answer,
                task_id=feedback_req.task_id,
                info=feedback_req.info,
            )
            self.logger.info(f"[Feedback memories result:] {feedback_result}")
        return feedback_result

    def _get_search_mode(self, mode: str) -> str:
        """
        Get search mode with environment variable fallback.

        Args:
            mode: Requested search mode

        Returns:
            Search mode string
        """
        return mode

    @timed
    def _search_text(
        self,
        search_req: APISearchRequest,
        user_context: UserContext,
        search_mode: str,
    ) -> list[dict[str, Any]]:
        """
        Search text memories based on mode.

        Args:
            search_req: Search request
            user_context: User context
            search_mode: Search mode (fast, fine, or mixture)

        Returns:
            List of formatted memory items
        """
        try:
            if search_mode == SearchMode.FAST:
                text_memories = self._fast_search(search_req, user_context)
            elif search_mode == SearchMode.FINE:
                text_memories = self._fine_search(search_req, user_context)
            elif search_mode == SearchMode.MIXTURE:
                text_memories = self._mix_search(search_req, user_context)
            else:
                self.logger.error(f"Unsupported search mode: {search_mode}")
                return []
            return text_memories

        except Exception as e:
            self.logger.error("Error in search_text: %s; traceback: %s", e, traceback.format_exc())
            return []

    def _deep_search(
        self,
        search_req: APISearchRequest,
        user_context: UserContext,
    ) -> list:
        target_session_id = search_req.session_id or "default_session"
        # Keep the owner scope (already merged into ``search_req.filter`` by
        # ``_with_owner_scope``); dropping it here let deep search read every
        # participant's private memory.
        search_filter = search_req.filter
        if search_req.session_id:
            session_filter = {"session_id": search_req.session_id}
            search_filter = (
                {"and": [search_filter, session_filter]} if search_filter else session_filter
            )

        info = {
            "user_id": search_req.user_id,
            "session_id": target_session_id,
            "chat_history": search_req.chat_history,
        }

        enhanced_memories = self.searcher.deep_search(
            query=search_req.query,
            user_name=user_context.mem_cube_id,
            top_k=search_req.top_k,
            mode=SearchMode.FINE,
            manual_close_internet=not search_req.internet_search,
            moscube=search_req.moscube,
            search_filter=search_filter,
            info=info,
        )
        return self._postformat_memories(
            enhanced_memories,
            user_context.mem_cube_id,
            include_embedding=_includes_embedding(search_req.dedup),
            neighbor_discovery=search_req.neighbor_discovery,
            save_sources=search_req.include_sources,
        )

    def _agentic_search(
        self, search_req: APISearchRequest, user_context: UserContext, max_thinking_depth: int
    ) -> list:
        deepsearch_results = self.deepsearch_agent.run(
            search_req.query, user_id=user_context.mem_cube_id
        )
        formatted = self._postformat_memories(
            deepsearch_results,
            user_context.mem_cube_id,
            include_embedding=_includes_embedding(search_req.dedup),
            neighbor_discovery=search_req.neighbor_discovery,
            save_sources=search_req.include_sources,
        )
        return self._filter_agentic_owner(formatted, search_req)

    def _filter_agentic_owner(
        self, memories: list[dict[str, Any]], search_req: APISearchRequest
    ) -> list[dict[str, Any]]:
        """Post-filter agentic results, which are not produced by our searcher.

        The agentic agent only receives a query, so owner scope has to be
        re-applied on the way out. Results without owner metadata are legacy
        nodes and stay visible; anything tagged with a foreign owner is dropped.
        """
        agent_ids = set(normalize_id_list(getattr(search_req, "agent_id", None)))
        group_id = getattr(search_req, "group_id", None)
        viewer_user_id = search_req.user_id.strip() if search_req.user_id else None
        allowed = {("user", viewer_user_id), ("public", self.cube_id)}
        allowed.update(("agent", agent_id) for agent_id in agent_ids)
        if group_id:
            allowed.add(("group", group_id))

        kept: list[dict[str, Any]] = []
        for memory in memories:
            metadata = memory.get("metadata") if isinstance(memory, dict) else None
            owner_type = (metadata or {}).get("owner_type")
            owner_id = (metadata or {}).get("owner_id")
            if owner_type:
                if (owner_type, owner_id) in allowed:
                    kept.append(memory)
                continue
            # Legacy node: apply the same compatibility-id rule as the graph
            # owner filter (``owner_type IS NULL`` + user_id/agent_id/group_id).
            legacy_user = (metadata or {}).get("user_id")
            legacy_agent = (metadata or {}).get("agent_id")
            legacy_group = (metadata or {}).get("group_id")
            if legacy_user and str(legacy_user) != viewer_user_id:
                continue
            if legacy_agent and str(legacy_agent) not in agent_ids:
                continue
            if legacy_group and str(legacy_group) != group_id:
                continue
            kept.append(memory)
        return kept

    def _fine_search(
        self,
        search_req: APISearchRequest,
        user_context: UserContext,
    ) -> list:
        """
        Fine-grained search with query enhancement.

        Args:
            search_req: Search request
            user_context: User context

        Returns:
            List of enhanced search results
        """
        # TODO: support tool memory search in future

        logger.info(f"Fine strategy: {FINE_STRATEGY}")
        if FINE_STRATEGY == FineStrategy.DEEP_SEARCH:
            return self._deep_search(search_req=search_req, user_context=user_context)
        elif FINE_STRATEGY == FineStrategy.AGENTIC_SEARCH:
            return self._agentic_search(search_req=search_req, user_context=user_context)

        target_session_id = search_req.session_id or "default_session"
        search_priority = {"session_id": search_req.session_id} if search_req.session_id else None
        search_filter = search_req.filter

        info = {
            "user_id": search_req.user_id,
            "session_id": target_session_id,
            "chat_history": search_req.chat_history,
        }

        # 与 fast 同一入口：偏好 / 技能 / 工具 / dream 的开关都在 search_req 上。
        raw_memories = search_text_memories(
            text_mem=self.naive_mem_cube.text_mem,
            search_req=search_req,
            user_context=user_context,
            mode=SearchMode.FINE,
            include_embedding=_includes_embedding(search_req.dedup),
        )

        # 偏好记忆不参与改写：按序号回填时模型少回几行会连 id 和 memory_type 一起丢。
        preference_memories = [
            mem for mem in raw_memories if mem.metadata.memory_type == "PreferenceMemory"
        ]
        text_memories = [
            mem for mem in raw_memories if mem.metadata.memory_type != "PreferenceMemory"
        ]

        # Enhance with query
        if text_memories:
            enhanced_memories, _ = self.mem_scheduler.retriever.enhance_memories_with_query(
                query_history=[search_req.query],
                memories=text_memories,
            )
        else:
            enhanced_memories = []
        enhanced_memories += preference_memories

        # 条数变少不代表信息缺失，按可用性判定是否补召回。
        gap_size = max(1, search_req.top_k // _GAP_RECALL_DEDUP_FACTOR)
        if not self._memories_can_answer(search_req.query, enhanced_memories, gap_size):
            missing_info_hint, trigger = self.mem_scheduler.retriever.recall_for_missing_memories(
                query=search_req.query,
                memories=[mem.memory for mem in enhanced_memories],
            )
            if trigger:
                logger.info(
                    f"Answerability insufficient; additional search with hint (top_k={gap_size}): {missing_info_hint}"
                )
                additional_memories = self.searcher.search(
                    query=missing_info_hint,
                    user_name=user_context.mem_cube_id,
                    top_k=gap_size,
                    mode=SearchMode.FAST,
                    memory_type=search_req.search_memory_type,
                    search_priority=search_priority,
                    search_filter=search_filter,
                    info=info,
                )
                enhanced_memories += additional_memories
                logger.info(
                    f"Added {len(additional_memories)} more memories. Total enhanced memories: {len(enhanced_memories)}"
                )
            else:
                logger.info(
                    "Answerability insufficient but no usable gap hint; keeping current memories."
                )
        else:
            logger.info("Answerability sufficient; skipping gap recall.")

        def _dedup_by_content(memories: list) -> list:
            seen = set()
            unique_memories = []
            for mem in memories:
                key = " ".join(mem.memory.split())
                if key in seen:
                    continue
                seen.add(key)
                unique_memories.append(mem)
            return unique_memories

        deduped_memories = (
            enhanced_memories if search_req.dedup == "no" else _dedup_by_content(enhanced_memories)
        )
        formatted_memories = self._postformat_memories(
            deduped_memories,
            user_context.mem_cube_id,
            include_embedding=_includes_embedding(search_req.dedup),
            neighbor_discovery=search_req.neighbor_discovery,
            save_sources=search_req.include_sources,
        )

        logger.info(f"Found {len(formatted_memories)} memories for user {search_req.user_id}")

        return formatted_memories

    def _memories_can_answer(self, query: str, memories: list, limit: int) -> bool:
        """候选够不够回答这个 query；只评估前 ``limit`` 条，评估失败按“够”处理。"""
        if not memories:
            return False
        try:
            return bool(
                self.mem_scheduler.retriever.evaluate_memory_answer_ability(
                    query=query,
                    memory_texts=[mem.memory for mem in memories],
                    top_k=limit,
                )
            )
        except Exception:
            logger.warning("[FineSearch] answerability evaluation failed", exc_info=True)
            return True

    def _fast_search(
        self,
        search_req: APISearchRequest,
        user_context: UserContext,
    ) -> list:
        """
        Fast search using vector database.

        Args:
            search_req: Search request
            user_context: User context

        Returns:
            List of search results
        """
        search_results = search_text_memories(
            text_mem=self.naive_mem_cube.text_mem,
            search_req=search_req,
            user_context=user_context,
            mode=SearchMode.FAST,
            include_embedding=_includes_embedding(search_req.dedup),
        )

        return self._postformat_memories(
            search_results,
            user_context.mem_cube_id,
            include_embedding=_includes_embedding(search_req.dedup),
            neighbor_discovery=search_req.neighbor_discovery,
            save_sources=search_req.include_sources,
        )

    def _postformat_memories(
        self,
        search_results: list,
        user_name: str,
        include_embedding: bool = False,
        neighbor_discovery: bool = False,
        save_sources: bool = True,
    ) -> list:
        """
        Postprocess search results.
        """

        def extract_edge_info(edges_info: list[dict], neighbor_relativity: float):
            edge_mems = []
            for edge in edges_info:
                chunk_target_id = edge.get("to")
                edge_type = edge.get("type")
                item_neighbor = self.searcher.graph_store.get_node(
                    chunk_target_id, user_name=user_name
                )
                if item_neighbor:
                    item_neighbor_mem = TextualMemoryItem(**item_neighbor)
                    item_neighbor_mem.metadata.relativity = neighbor_relativity
                    edge_mems.append(item_neighbor_mem)
                    item_neighbor_id = item_neighbor.get("id", "None")
                    self.logger.info(
                        f"Add neighbor chunk: {item_neighbor_id}, edge_type: {edge_type} for {item.id}"
                    )
            return edge_mems

        final_items = []
        if neighbor_discovery:
            for item in search_results:
                if item.metadata.memory_type == "RawFileMemory":
                    neighbor_relativity = item.metadata.relativity * 0.8
                    preceding_info = self.searcher.graph_store.get_edges(
                        item.id, type="PRECEDING", direction="OUTGOING", user_name=user_name
                    )
                    final_items.extend(extract_edge_info(preceding_info, neighbor_relativity))

                    final_items.append(item)

                    following_info = self.searcher.graph_store.get_edges(
                        item.id, type="FOLLOWING", direction="OUTGOING", user_name=user_name
                    )
                    final_items.extend(extract_edge_info(following_info, neighbor_relativity))

                else:
                    final_items.append(item)
        else:
            final_items = search_results

        return [
            format_memory_item(data, include_embedding=include_embedding, save_sources=save_sources)
            for data in final_items
        ]

    def _mix_search(
        self,
        search_req: APISearchRequest,
        user_context: UserContext,
    ) -> list:
        """
        Mix search combining fast and fine-grained approaches.

        Args:
            search_req: Search request
            user_context: User context

        Returns:
            List of formatted search results
        """
        return self.mem_scheduler.mix_search_memories(
            search_req=search_req,
            user_context=user_context,
        )

    def _get_sync_mode(self) -> str:
        """
        Get synchronization mode from memory cube.

        Returns:
            Sync mode string ("sync" or "async")
        """
        try:
            return getattr(self.naive_mem_cube.text_mem, "mode", "sync")
        except Exception:
            return "sync"

    def _schedule_memory_tasks(
        self,
        add_req: APIADDRequest,
        user_context: UserContext,
        mem_ids: list[str],
        sync_mode: str,
    ) -> None:
        """
        Schedule memory processing tasks based on sync mode.

        Args:
            add_req: Add memory request
            user_context: User context
            mem_ids: List of memory IDs
            sync_mode: Synchronization mode
        """
        target_session_id = add_req.session_id or "default_session"

        if sync_mode == "async":
            # Async mode: submit MEM_READ_LABEL task
            try:
                message_item_read = ScheduleMessageItem(
                    user_id=add_req.user_id,
                    task_id=add_req.task_id,
                    session_id=target_session_id,
                    mem_cube_id=self.cube_id,
                    mem_cube=self.naive_mem_cube,
                    label=MEM_READ_TASK_LABEL,
                    content=json.dumps(mem_ids),
                    timestamp=utc_now(),
                    user_name=self.cube_id,
                    info={
                        **(add_req.info or {}),
                        "custom_tags": add_req.custom_tags,
                        "custom_tags_match": getattr(add_req, "custom_tags_match", None),
                        "custom_extract_prompt": getattr(add_req, "custom_extract_prompt", None),
                    },
                    chat_history=add_req.chat_history,
                    user_context=user_context,
                )
                self.mem_scheduler.submit_messages(messages=[message_item_read])
                self.logger.info(
                    f"[SingleCubeView] cube={self.cube_id} Submitted async MEM_READ: {json.dumps(mem_ids)}"
                )
            except Exception as e:
                self.logger.error(
                    f"[SingleCubeView] cube={self.cube_id} Failed to submit async memory tasks: {e}",
                    exc_info=True,
                )
        else:
            message_item_add = ScheduleMessageItem(
                user_id=add_req.user_id,
                task_id=add_req.task_id,
                session_id=target_session_id,
                mem_cube_id=self.cube_id,
                mem_cube=self.naive_mem_cube,
                label=ADD_TASK_LABEL,
                content=json.dumps(mem_ids),
                timestamp=utc_now(),
                user_name=self.cube_id,
            )
            self.mem_scheduler.submit_messages(messages=[message_item_add])

    @timed
    def extract_memories(self, add_req: APIADDRequest) -> list[dict[str, Any]]:
        """Run the reader extraction pipeline without persisting anything.

        This backs the ``/product/extract_memory`` dry-run endpoint: it answers
        "what would be extracted from these messages?" without writing to the
        graph, the vector store, or the scheduler.
        """
        sync_mode = getattr(add_req, "async_mode", None) or "sync"
        user_context = UserContext(
            user_id=add_req.user_id,
            mem_cube_id=self.cube_id,
            session_id=add_req.session_id or "default_session",
            manager_user_id=add_req.manager_user_id,
            project_id=add_req.project_id,
        )

        flattened_local, _, _ = self._extract_text_mem(add_req, user_context, sync_mode)
        return [
            {
                **format_memory_item(memory, include_embedding=False, save_sources=True),
                "cube_id": self.cube_id,
            }
            for memory in flattened_local
            if memory.metadata.memory_type != "RawFileMemory"
        ]

    def _resolve_extract_mode(self, add_req: APIADDRequest, sync_mode: str) -> str:
        # async: always fast (ignore add_req.mode)
        # sync: use add_req.mode == "fast" to switch to fast pipeline, otherwise fine
        if sync_mode == "async":
            return "fast"
        return "fast" if add_req.mode == "fast" else "fine"

    @staticmethod
    def _apply_chat_time(memories: list[TextualMemoryItem]) -> None:
        """Propagate the message-level ``chat_time`` onto memory metadata.

        The reader keeps ``chat_time`` on each ``SourceMessage`` only, which
        makes it impossible to filter or sort by event time after the fact.
        Copy the first non-empty source value and normalize it to UTC ISO when
        the value is parseable; keep the original string otherwise.
        """
        for memory in memories:
            if getattr(memory.metadata, "chat_time", None):
                continue
            raw = None
            for source in memory.metadata.sources or []:
                raw = getattr(source, "chat_time", None)
                if raw:
                    break
            if raw:
                memory.metadata.chat_time = normalize_to_utc_iso(raw) or str(raw)

    @staticmethod
    def _fill_source_fields(memories: list[TextualMemoryItem], session_id: str) -> None:
        """Give every chat source the cloud fields ``lang`` and ``message_id``."""
        from memos.api.message_store import message_id_for
        from memos.memories.textual.tree_text_memory.retrieve.retrieve_utils import detect_lang

        for memory in memories:
            for source in getattr(memory.metadata, "sources", None) or []:
                if getattr(source, "type", None) != "chat":
                    continue
                content = getattr(source, "content", None) or memory.memory
                if not getattr(source, "lang", None):
                    source.lang = detect_lang(content)
                if not getattr(source, "message_id", None):
                    source.message_id = message_id_for(
                        session_id, getattr(source, "role", None) or "", content
                    )

    def _extract_text_mem(
        self,
        add_req: APIADDRequest,
        user_context: UserContext,
        sync_mode: str,
        owner_view: OwnerView | None = None,
    ) -> tuple[list[TextualMemoryItem], str, int]:
        """Run the reader and return ``(memories, extract_mode, get_memory_ms)``."""
        target_session_id = add_req.session_id or "default_session"
        extract_mode = self._resolve_extract_mode(add_req, sync_mode)
        self.logger.info(
            "[SingleCubeView] cube=%s Processing text memory "
            "with sync_mode=%s, extract_mode=%s, add_mode=%s",
            user_context.mem_cube_id,
            sync_mode,
            extract_mode,
            add_req.mode,
        )

        raw_messages = getattr(add_req, "raw_messages", None)
        messages = raw_messages if raw_messages is not None else add_req.messages

        # Stage 1+2: parse + embedding (logged inside get_memory via timed_stage)
        with timed_stage("add", "get_memory", cube_id=self.cube_id) as ts_gm:
            memories_local = self.mem_reader.get_memory(
                [messages],
                type="chat",
                info={
                    **(add_req.info or {}),
                    "custom_tags": add_req.custom_tags,
                    "user_id": add_req.user_id,
                    "session_id": target_session_id,
                },
                mode=extract_mode,
                user_name=user_context.mem_cube_id,
                chat_history=add_req.chat_history,
                user_context=user_context,
            )

        flattened_local = [mm for m in memories_local for mm in m]
        self._apply_chat_time(flattened_local)
        self._fill_source_fields(flattened_local, target_session_id)
        apply_semantic_custom_tags(
            flattened_local,
            add_req.custom_tags,
            getattr(self.mem_reader, "embedder", None),
            enabled=getattr(add_req, "custom_tags_match", None),
        )

        # Event memory is an opt-in view (cloud: include "event" in
        # `allow_memory_view`), so it never adds an LLM call to existing traffic.
        allow_views = getattr(add_req, "allow_memory_view", None)
        if allow_views is not None and "event" in allow_views:
            flattened_local.extend(self._extract_event_memories(add_req, user_context, sync_mode))

        # Write-side generation whitelist (cloud `allow_memory_view`).
        flattened_local = filter_by_allow_memory_view(flattened_local, allow_views)

        # Owner stamping + lifecycle fields. Done before the write so every
        # backend (graph property, vector payload) sees the same values.
        if owner_view is not None:
            for memory in flattened_local:
                stamp_owner(memory, owner_view, task_id=add_req.task_id)
        expires_at = getattr(add_req, "expires_at", None)
        if expires_at:
            for memory in flattened_local:
                memory.metadata.expires_at = normalize_to_utc_iso(expires_at) or expires_at

        # Explicitly set source_doc_id to metadata if present in info
        source_doc_id = (add_req.info or {}).get("source_doc_id")
        if source_doc_id:
            for memory in flattened_local:
                memory.metadata.source_doc_id = source_doc_id

        return flattened_local, extract_mode, ts_gm.duration_ms

    def _extract_event_memories(
        self,
        add_req: APIADDRequest,
        user_context: UserContext,
        sync_mode: str,
    ) -> list[TextualMemoryItem]:
        from memos.mem_reader.event_extractor import EventExtractor

        llm = getattr(self.mem_reader, "general_llm", None) or getattr(self.mem_reader, "llm", None)
        raw_messages = getattr(add_req, "raw_messages", None)
        messages = raw_messages if raw_messages is not None else add_req.messages
        if llm is None or not isinstance(messages, list):
            return []
        extractor = EventExtractor(llm)
        try:
            return extractor.extract(
                messages,
                info={
                    **(add_req.info or {}),
                    "user_id": user_context.user_id,
                    "custom_tags": add_req.custom_tags,
                },
                user_name=user_context.mem_cube_id,
                session_id=add_req.session_id,
                need_embed=True,
                embedder=getattr(self.mem_reader, "embedder", None),
            )
        except Exception:
            self.logger.warning("Event extraction failed", exc_info=True)
            return []

    @timed
    def _process_text_mem(
        self,
        add_req: APIADDRequest,
        user_context: UserContext,
        sync_mode: str,
        owner_view: OwnerView | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process and add text memories (including preference memories).

        Extracts memories from messages and adds them to the text memory system.
        Handles both sync and async modes.

        Args:
            add_req: Add memory request
            user_context: User context with IDs

        Returns:
            List of formatted memory responses
        """
        process_start = time.perf_counter()

        flattened_local, extract_mode, get_memory_ms = self._extract_text_mem(
            add_req, user_context, sync_mode, owner_view=owner_view
        )

        # Add memories to text_mem
        mem_group = [
            memory for memory in flattened_local if memory.metadata.memory_type != "RawFileMemory"
        ]

        # Two-stage add (default off): make the coarse view searchable first.
        two_stage = two_stage_enabled() and bool(add_req.task_id)
        if two_stage and mem_group:
            coarse_items = build_coarse_items(
                mem_group,
                task_id=add_req.task_id,
                scope=f"{user_context.mem_cube_id}:{add_req.user_id or ''}:{getattr(add_req, 'agent_id', None) or ''}",
            )
            try:
                self.naive_mem_cube.text_mem.add(coarse_items, user_name=user_context.mem_cube_id)
                self.logger.info(
                    f"[SingleCubeView] two-stage: wrote {len(coarse_items)} coarse memories "
                    f"for task {add_req.task_id}"
                )
            except Exception:
                self.logger.warning("Failed to write coarse memories", exc_info=True)

        # Stage 3: write_db
        with timed_stage("add", "write_db", cube_id=self.cube_id) as ts_db:
            mem_ids_local: list[str] = _as_memory_ids(
                self.naive_mem_cube.text_mem.add(
                    mem_group,
                    user_name=user_context.mem_cube_id,
                )
            )

            self.logger.info(
                f"Added {len(mem_ids_local)} memories for user {add_req.user_id} "
                f"in session {add_req.session_id}: {mem_ids_local}"
            )

            if two_stage:
                archive_coarse_nodes(
                    getattr(self.naive_mem_cube.text_mem, "graph_store", None),
                    user_name=user_context.mem_cube_id,
                    task_id=add_req.task_id,
                    fine_memories=mem_group,
                )

            # Add raw file nodes and edges
            if self.mem_reader.save_rawfile and extract_mode == "fine":
                raw_file_mem_group = [
                    memory
                    for memory in flattened_local
                    if memory.metadata.memory_type == "RawFileMemory"
                ]
                self.naive_mem_cube.text_mem.add_rawfile_nodes_n_edges(
                    raw_file_mem_group,
                    mem_ids_local,
                    user_id=add_req.user_id,
                    user_name=user_context.mem_cube_id,
                )
            ts_db.set(memory_count=len(mem_ids_local))
        write_db_ms = ts_db.duration_ms

        # Stage 4: schedule
        with timed_stage("add", "schedule", cube_id=self.cube_id) as ts_sched:
            self._schedule_memory_tasks(
                add_req=add_req,
                user_context=user_context,
                mem_ids=mem_ids_local,
                sync_mode=sync_mode,
            )

            # Mark merged_from memories as archived when provided in add_req.info
            if (
                sync_mode == "sync"
                and extract_mode == "fine"
                and (
                    not hasattr(self.mem_reader, "memory_version_switch")
                    or self.mem_reader.memory_version_switch != "on"
                )
            ):
                for memory in flattened_local:
                    merged_from = (memory.metadata.info or {}).get("merged_from")
                    if merged_from:
                        old_ids = (
                            merged_from
                            if isinstance(merged_from, (list | tuple | set))
                            else [merged_from]
                        )
                        if self.mem_reader and self.mem_reader.graph_db:
                            for old_id in old_ids:
                                try:
                                    self.mem_reader.graph_db.update_node(
                                        str(old_id),
                                        {"status": "archived"},
                                        user_name=user_context.mem_cube_id,
                                    )
                                    self.logger.info(
                                        f"[SingleCubeView] Archived merged_from memory: {old_id}"
                                    )
                                except Exception as e:
                                    self.logger.warning(
                                        f"[SingleCubeView] Failed to archive merged_from memory {old_id}: {e}"
                                    )
                        else:
                            self.logger.warning(
                                "[SingleCubeView] merged_from provided but graph_db is unavailable; skip archiving."
                            )
        schedule_ms = ts_sched.duration_ms

        # Summary rollup — total_ms is the outer wall-clock, not a new stage
        total_ms = int((time.perf_counter() - process_start) * 1000)
        input_msg_count = len(add_req.messages) if add_req.messages else 0
        memory_count = len(mem_ids_local)
        est_input_tokens = (
            sum(
                len(str(m.get("content", ""))) if isinstance(m, dict) else len(str(m))
                for m in (add_req.messages or [])
            )
            // 4
        )
        timed_stage.emit_now(
            "add",
            "summary",
            cube_id=self.cube_id,
            sync_mode=sync_mode,
            extract_mode=extract_mode,
            input_msg_count=input_msg_count,
            est_input_tokens=est_input_tokens,
            memory_count=memory_count,
            get_memory_ms=get_memory_ms,
            write_db_ms=write_db_ms,
            schedule_ms=schedule_ms,
            total_ms=total_ms,
            per_item_ms=total_ms // max(memory_count, 1),
        )

        # Format results uniformly
        text_memories = [
            {
                "memory": memory.memory,
                "memory_id": memory_id,
                "memory_type": memory.metadata.memory_type,
                "cube_id": self.cube_id,
            }
            for memory_id, memory in zip(mem_ids_local, mem_group, strict=False)
        ]

        return text_memories
