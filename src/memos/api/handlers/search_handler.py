"""
Search handler for memory search functionality (Class-based version).

This module provides a class-based implementation of search handlers,
using dependency injection for better modularity and testability.
"""

import copy
import json
import math
import os

from contextlib import suppress
from typing import Any

from fastapi import HTTPException

from memos.api.handlers.base_handler import BaseHandler, HandlerDependencies
from memos.api.handlers.formatters_handler import rerank_knowledge_mem
from memos.api.product_models import APISearchRequest, SearchResponse
from memos.dream.contextualization import CONTEXT_MEMORY_TYPE
from memos.log import get_logger, summarize_search_request, summarize_search_results
from memos.memories.textual.tree_text_memory.retrieve.retrieve_utils import (
    cosine_similarity_matrix,
)
from memos.multi_mem_cube.composite_cube import CompositeCubeView
from memos.multi_mem_cube.single_cube import SingleCubeView
from memos.multi_mem_cube.views import MemCubeView
from memos.plugins.hook_defs import H
from memos.plugins.hooks import has_hook, hookable, trigger_hook
from memos.search.context_format import (
    VALID_MODES,
    apply_context_format,
    normalize_mode,
)
from memos.search.time_intent import time_aware_enabled
from memos.search.time_understanding import TimeIntent, understand_query
from memos.search.version_select import apply_time_intent, widened_top_k


logger = get_logger(__name__)

_ENV_CONTEXT_RECALL = "MEMOS_DREAM_CONTEXT_RECALL"
_ENV_CONTEXT_RECALL_TOP_K = "MEMOS_DREAM_CONTEXT_RECALL_TOP_K"
_DEFAULT_CONTEXT_RECALL_TOP_K = 2
_ENV_MMR_CANDIDATE_PRUNING = "MEMOS_MMR_CANDIDATE_PRUNING_ENABLED"
_MMR_CANDIDATE_MULTIPLIER = 2


def _env_enabled(name: str, default: str = "off") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    with suppress(TypeError, ValueError):
        return int(os.getenv(name, str(default)))
    return default


def _env_float(name: str, default: float) -> float:
    with suppress(TypeError, ValueError):
        return float(os.getenv(name, str(default)))
    return default


def _history_versions(raw: Any) -> list[dict[str, Any]]:
    """Normalize a node's ``history`` into ``[{version, memory, ...}]``, newest first."""
    if isinstance(raw, str):
        with suppress(ValueError):
            raw = json.loads(raw)
    if not isinstance(raw, list):
        return []
    versions: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str):
            with suppress(ValueError):
                entry = json.loads(entry)
        if not isinstance(entry, dict):
            continue
        text = entry.get("memory")
        if not isinstance(text, str) or not text.strip():
            continue
        version = {
            "version": entry.get("version"),
            "memory": text,
            "update_type": entry.get("update_type"),
            "created_at": entry.get("created_at"),
        }
        if entry.get("timespec"):
            version["timespec"] = entry["timespec"]
        versions.append(version)
    versions.sort(
        key=lambda v: (
            v["version"] if isinstance(v["version"], int) else -1,
            str(v["created_at"] or ""),
        ),
        reverse=True,
    )
    return versions


class SearchHandler(BaseHandler):
    """
    Handler for memory search operations.

    Provides fast, fine-grained, and mixture-based search modes.
    """

    def __init__(self, dependencies: HandlerDependencies):
        """
        Initialize search handler.

        Args:
            dependencies: HandlerDependencies instance
        """
        super().__init__(dependencies)
        self._validate_dependencies(
            "naive_mem_cube", "mem_scheduler", "searcher", "deepsearch_agent"
        )

    @hookable("search")
    def handle_search_memories(self, search_req: APISearchRequest) -> SearchResponse:
        """
        Main handler for search memories endpoint.

        Orchestrates the search process based on the requested search mode,
        supporting text memory searches.

        Args:
            search_req: Search request containing query and parameters

        Returns:
            SearchResponse with formatted results
        """
        self.logger.info(
            "[SearchHandler] Search request summary: %s",
            summarize_search_request(search_req),
        )

        context_mode = self._context_mode(search_req)

        # Use deepcopy to avoid modifying the original request object
        search_req_local = copy.deepcopy(search_req)
        intent = self._apply_time_intent(search_req_local)

        # A time window is applied to the hits (version selection and rerank), not to
        # the candidate query, so retrieval needs more candidates to still fill top_k.
        windowed = intent is not None and intent.kind in ("as_of", "recent")
        finish_req = search_req
        if windowed:
            finish_req = search_req.model_copy(update={"top_k": widened_top_k(search_req.top_k)})
        search_req_local.top_k = finish_req.top_k

        # Expand top_k for deduplication (5x to ensure enough candidates)
        if search_req_local.dedup in ("sim", "mmr"):
            search_req_local.top_k = search_req_local.top_k * 3

        # Search and deduplicate
        cube_view = self._build_cube_view(search_req_local)
        results = self._finish_search(
            finish_req, search_req_local, cube_view.search_memories(search_req_local)
        )
        if intent is not None:
            results = apply_time_intent(
                results,
                intent,
                top_k=search_req.top_k,
                rerank=not search_req.sort_by,
            )
        results = apply_context_format(results, search_req.query, context_mode)

        self.logger.info(
            "[SearchHandler] Final search result summary: %s",
            summarize_search_results(results),
        )

        return SearchResponse(
            message="Search completed successfully",
            data=results,
        )

    def _finish_search(
        self,
        search_req: APISearchRequest,
        search_req_local: APISearchRequest,
        results: dict[str, Any],
    ) -> dict[str, Any]:
        """Hooks, thresholds, dedup, rerank, view filters and sorting for one search."""
        hooked_results = trigger_hook(
            H.SEARCH_MEMORY_RESULTS,
            handler=self,
            search_req=search_req_local,
            results=results,
        )
        if hooked_results is not None:
            results = hooked_results
        if not search_req_local.relativity:
            search_req_local.relativity = 0
        self.logger.info(f"[SearchHandler] Relativity filter: {search_req_local.relativity}")
        results = self._apply_relativity_threshold(results, search_req_local.relativity)

        if search_req_local.dedup == "sim":
            results = self._dedup_text_memories(results, search_req.top_k)
            self._strip_embeddings(results)
        elif search_req_local.dedup == "mmr":
            pref_top_k = getattr(search_req_local, "pref_top_k", 6)
            results = self._mmr_dedup_text_memories(results, search_req.top_k, pref_top_k)
            self._strip_embeddings(results)

        text_mem = results["text_mem"]
        results["text_mem"] = rerank_knowledge_mem(
            self.reranker,
            query=search_req.query,
            text_mem=text_mem,
            top_k=search_req_local.top_k,
            file_mem_proportion=0.5,
        )
        hooked_results = trigger_hook(
            H.SEARCH_RESULTS_AFTER_RERANK,
            handler=self,
            search_req=search_req_local,
            results=results,
        )
        if hooked_results is not None:
            results = hooked_results

        hooked_results = trigger_hook(
            H.SEARCH_CONTEXT_RENDER,
            handler=self,
            search_req=search_req_local,
            results=results,
        )
        if hooked_results is not None:
            results = hooked_results

        # Cloud-compatible recall whitelist + profile recall.
        results = self._filter_by_include_memory_view(results, search_req_local.include_memory_view)
        results = self._append_profile_memories(results, search_req_local)

        if getattr(search_req_local, "_budget_across_views", False):
            # `search_req.top_k` here is the caller's value, never the 3x dedup
            # widening on `search_req_local`. With a time window it is the widened
            # top_k, and `apply_time_intent` only trims text_mem back.
            results = self._apply_view_budget(results, search_req.top_k)

        # 放在最后一道：钩子也可能往 results 里塞东西，出栈前统一只留 activated。
        results = self._drop_inactive(results)
        results = self._sort_memories(
            results,
            sort_by=search_req_local.sort_by,
            sort_order=search_req_local.sort_order,
        )
        if search_req_local.include_history:
            results = self._attach_history_versions(results)
        return results

    @staticmethod
    def _context_mode(search_req: APISearchRequest) -> str:
        """The built-in context format; a value no built-in knows belongs to a render plugin."""
        mode = normalize_mode(search_req.context_format)
        if mode is not None:
            return mode
        if has_hook(H.SEARCH_CONTEXT_RENDER):
            return "memory"
        raise HTTPException(
            status_code=422,
            detail=(
                f"unsupported context_format {search_req.context_format!r}; "
                f"expected one of: {', '.join(VALID_MODES)}"
            ),
        )

    def _apply_time_intent(self, search_req: APISearchRequest) -> TimeIntent | None:
        """What the query says about time; ``None`` when time awareness is off.

        Opt-in (``time_aware`` or ``SEARCH_TIME_AWARE``). The candidate query itself
        is never narrowed: the intent is applied to the hits afterwards (see
        ``version_select``). A query that only asks about the past ("我以前住哪")
        gets the earlier versions of each hit attached.
        """
        if not time_aware_enabled(search_req.time_aware):
            return None
        intent = understand_query(search_req.query, getattr(self.deps, "llm", None))
        if intent.kind == "past":
            search_req.include_history = True
        self.logger.info(
            "[SearchHandler] Time intent: %s (%s) %s", intent.kind, intent.source, intent.label
        )
        return intent

    @staticmethod
    def _attach_history_versions(results: dict[str, Any]) -> dict[str, Any]:
        """Expose earlier versions of each hit as ``metadata.history_versions``.

        The versions already live on the node (`history`, written by
        ``update_memory``, feedback and the version pipeline); this only gives
        them a stable shape. Archived nodes are not searched, so the result set
        and ``top_k`` stay exactly as they were.
        """
        limit = max(1, _env_int("SEARCH_HISTORY_MAX_VERSIONS", 10))
        for bucket in ("text_mem", "event_mem", "tool_mem"):
            for group in (results or {}).get(bucket) or []:
                memories = group.get("memories") if isinstance(group, dict) else None
                for memory in memories or []:
                    metadata = memory.get("metadata") if isinstance(memory, dict) else None
                    if not isinstance(metadata, dict):
                        continue
                    versions = _history_versions(metadata.get("history"))
                    if versions:
                        metadata["history_versions"] = versions[:limit]
        return results

    @staticmethod
    def _is_profile_item(item: dict[str, Any]) -> bool:
        """Keep in sync with ``cloud_contract.split_views`` (same profile detection)."""
        metadata = item.get("metadata")
        return bool(item.get("template_id")) or (
            isinstance(metadata, dict) and metadata.get("memory_type") == "ProfileMemory"
        )

    @staticmethod
    def _relativity(item: dict[str, Any]) -> float:
        """Relativity from the item or its metadata; 0.0 when absent or not a finite number."""
        for source in (item, item.get("metadata")):
            value = source.get("relativity") if isinstance(source, dict) else None
            if value is None:
                continue
            try:
                score = float(value)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return score if math.isfinite(score) else 0.0
        return 0.0

    @staticmethod
    def _apply_view_budget(results: dict[str, Any], budget: int) -> dict[str, Any]:
        """Keep the best ``budget`` hits across every view (cloud `memory_limit_number`).

        The cloud returns one global budget split into per-view lists, e.g.
        ``memory_limit_number=3`` gives 1 factual + 2 preference. Retrieval already
        over-fetches per view, so this only trims.
        """
        buckets = ("text_mem", "pref_mem", "event_mem", "tool_mem", "profile_mem")
        candidates: list[tuple[float, str, int, int]] = []
        profile_keys: set[tuple[str, int, int]] = set()
        for bucket in buckets:
            for group_index, group in enumerate(results.get(bucket) or []):
                if not isinstance(group, dict):
                    continue
                for item_index, item in enumerate(group.get("memories") or []):
                    key = (bucket, group_index, item_index)
                    if SearchHandler._is_profile_item(item):
                        # Profile fields have no relevance score (ProfileService.recall
                        # leaves `relativity` None), so ranking them against scored hits
                        # would always drop them. The budget trims recall views only.
                        profile_keys.add(key)
                        continue
                    candidates.append((SearchHandler._relativity(item), *key))
        if len(candidates) <= budget:
            return results
        keep = profile_keys | {tuple(key) for _, *key in sorted(candidates, reverse=True)[:budget]}
        for bucket in buckets:
            groups = []
            for group_index, group in enumerate(results.get(bucket) or []):
                if not isinstance(group, dict):
                    continue
                memories = [
                    item
                    for item_index, item in enumerate(group.get("memories") or [])
                    if (bucket, group_index, item_index) in keep
                ]
                if memories:
                    groups.append({**group, "memories": memories})
            if results.get(bucket):
                results[bucket] = groups
        return results

    @staticmethod
    def _filter_by_include_memory_view(
        results: dict[str, Any], views: list[str] | None
    ) -> dict[str, Any]:
        """Keep only the buckets named in ``include_memory_view``.

        Legacy switches (`include_preference` etc.) were already mapped onto
        the request by the model validator; this is the bucket-level gate.
        """
        if views is None or not results:
            return results

        bucket_to_view = {
            "text_mem": "detail_factual",
            "pref_mem": "preference",
            "event_mem": "event",
            "tool_mem": "tool_memory",
            "profile_mem": "profile",
        }
        for bucket, group in list(results.items()):
            if bucket in ("pref_note", "pref_string"):
                if "preference" not in views:
                    results[bucket] = "" if isinstance(group, str) else []
                continue
            if bucket == "dream_mem":
                # Dream synthesis is not one of the cloud views.
                results[bucket] = []
                continue
            view = bucket_to_view.get(bucket)
            if view is not None and view not in views and isinstance(group, list):
                results[bucket] = []

        if "preference" not in views and isinstance(results.get("pref_note"), str):
            results["pref_note"] = ""
        return results

    def _append_profile_memories(
        self, results: dict[str, Any], search_req: APISearchRequest
    ) -> dict[str, Any]:
        """Merge bound profile instances into the response (cloud `profile` view)."""
        views = search_req.include_memory_view
        if not views or "profile" not in views or results is None:
            return results

        from memos.api.cloud_store import build_metadata_store
        from memos.api.profile import ProfileService, profile_reserved_top_k
        from memos.memories.textual.owner import normalize_id_list

        graph_db = self.graph_db or getattr(self.searcher, "graph_store", None)
        try:
            service = ProfileService(build_metadata_store(graph_db))
        except Exception:
            self.logger.warning("Profile recall unavailable", exc_info=True)
            return results

        top_k = profile_reserved_top_k()
        if top_k <= 0:
            return results

        agent_ids = normalize_id_list(getattr(search_req, "agent_id", None))
        subjects: list[dict[str, str]] = []
        if search_req.user_id:
            subjects.append({"user_id": search_req.user_id})
        subjects.extend({"agent_id": aid} for aid in agent_ids)
        profile_groups: list[dict[str, Any]] = []
        for cube_id in self._resolve_cube_ids(search_req):
            memories: list[dict[str, Any]] = []
            for subject in subjects:
                memories.extend(
                    service.recall(
                        user_name=cube_id,
                        top_k=top_k,
                        **subject,
                    )
                )
            if memories:
                profile_groups.append(
                    {
                        "cube_id": cube_id,
                        "memories": memories,
                        "total_nodes": len(memories),
                    }
                )
        if profile_groups:
            results.setdefault("profile_mem", []).extend(profile_groups)
        return results

    def _recall_context_buckets(
        self, *, search_req: APISearchRequest, top_k: int
    ) -> list[dict[str, Any]]:
        graph_db = self.graph_db or getattr(self.searcher, "graph_store", None)
        embedder = self.embedder or getattr(self.searcher, "embedder", None)
        if graph_db is None or embedder is None:
            self.logger.info(
                "[SearchHandler] Context recall skipped: graph_db or embedder unavailable."
            )
            return []

        try:
            query_embedding = embedder.embed([search_req.query])[0]
        except Exception:
            self.logger.warning("[SearchHandler] Context recall embedding failed.", exc_info=True)
            return []

        buckets: list[dict[str, Any]] = []
        for cube_id in self._resolve_cube_ids(search_req):
            try:
                hits = graph_db.search_by_embedding(
                    query_embedding,
                    top_k=top_k,
                    scope=CONTEXT_MEMORY_TYPE,
                    status="activated",
                    user_name=cube_id,
                    return_fields=[
                        "memory",
                        "key",
                        "created_at",
                        "updated_at",
                        "source",
                        "internal_info",
                        # 出栈过滤（_drop_inactive）按 status 判定，这里不回填的话
                        # 整类 Context 命中都会被当成"状态未知"误杀。
                        "status",
                    ],
                )
            except Exception:
                self.logger.warning(
                    "[SearchHandler] Context recall search failed for cube=%s.",
                    cube_id,
                    exc_info=True,
                )
                continue

            memories = [self._format_context_hit(hit) for hit in hits or [] if hit.get("memory")]
            if not memories:
                continue
            buckets.append(
                {
                    "cube_id": cube_id,
                    "memories": memories,
                    "total_nodes": len(memories),
                }
            )
        return buckets

    @staticmethod
    def _format_context_hit(hit: dict[str, Any]) -> dict[str, Any]:
        context_id = str(hit.get("id", ""))
        score = float(hit.get("score", 0.0) or 0.0)
        metadata = {
            "id": context_id,
            "memory": hit.get("memory", ""),
            "memory_type": CONTEXT_MEMORY_TYPE,
            "source": hit.get("source") or "dream",
            "key": hit.get("key", ""),
            "relativity": score,
            "score": score,
            "embedding": [],
            "sources": [],
            "usage": [],
            "ref_id": f"[{context_id.split('-')[0]}]" if context_id else "[context]",
        }
        # 这条路径在调用处已经用 status="activated" 过滤过，所以状态是确定的；
        # 回填只是为了让下游（出栈过滤、插件）能自己判断，而不是靠"这条路径一定干净"的默契。
        metadata["status"] = hit.get("status") or "activated"
        for field in ("created_at", "updated_at", "internal_info"):
            if hit.get(field) is not None:
                metadata[field] = hit[field]

        return {
            "id": context_id,
            "memory": hit.get("memory", ""),
            "metadata": metadata,
            "ref_id": metadata["ref_id"],
        }

    @staticmethod
    def _drop_inactive(results: dict[str, Any]) -> dict[str, Any]:
        """出栈前只留 activated 的记忆（two-stage 开启时放行 resolving）。

        候选集那一层已经各自限定过 status（graph recall 走 `get_by_metadata(status=…)`、
        向量走 Qdrant 的 payload 过滤），这里是兜底：只有 activated 对调用方有意义，
        归档的是被合并掉的旧版本、软删的是用户已经删掉的，返回它们只会挤占 top_k
        并让调用方拿到重复或已失效的内容。

        两段式加工（`TWO_STAGE_ADD=true`）下，粗加工节点是 `resolving`，需要在精加工
        完成前可见；它们的 relativity 会乘一个较低权重（`TWO_STAGE_COARSE_WEIGHT`，
        默认 0.8），避免粗结果压过精结果。

        拿不到 status 的按不可返回处理（fail-closed）——无法证明它是 activated。
        这类会打成 WARNING，否则某个上游漏传 status 时会变成静默的召回下跌。

        例外：dream_mem（日度跨案例综合 + 洞察）不走这条判定。它的终态就是 completed，
        context_only / skipped 同样是终态，不是"被归档"或"被删除"。这里按 activated 过滤
        会把整个桶清空，所以整桶放行；这两个类型的生命周期由 Dream 侧自己管。
        """
        if not results:
            return results

        from memos.memories.textual.two_stage import two_stage_enabled

        allow_resolving = two_stage_enabled()
        coarse_weight = _env_float("TWO_STAGE_COARSE_WEIGHT", 0.8)

        dropped: dict[str, int] = {}
        unknown: list[str] = []
        for key, groups in results.items():
            if key == "dream_mem":
                continue
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                memories = group.get("memories")
                if not isinstance(memories, list):
                    continue
                kept = []
                for mem in memories:
                    meta = mem.get("metadata") if isinstance(mem, dict) else None
                    status = meta.get("status") if isinstance(meta, dict) else None
                    if status == "activated":
                        kept.append(mem)
                        continue
                    if status == "resolving" and allow_resolving:
                        relativity = meta.get("relativity")
                        if isinstance(relativity, int | float):
                            meta["relativity"] = relativity * coarse_weight
                        kept.append(mem)
                        continue
                    if status is None:
                        unknown.append(str(mem.get("id", "?")) if isinstance(mem, dict) else "?")
                    else:
                        dropped[status] = dropped.get(status, 0) + 1
                group["memories"] = kept
                if "total_nodes" in group:
                    group["total_nodes"] = len(kept)

        if dropped:
            logger.info("[SearchHandler] Dropped non-activated memories: %s", dropped)
        if unknown:
            logger.warning(
                "[SearchHandler] Dropped %d memory(ies) with no status (fail-closed): %s",
                len(unknown),
                unknown[:10],
            )
        return results

    @staticmethod
    def _sort_memories(
        results: dict[str, Any],
        sort_by: str | None,
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """Sort each bucket's memories by a metadata field (missing values last)."""
        if not sort_by or not results:
            return results

        from memos.utils import parse_datetime

        def sort_key(memory: dict[str, Any]) -> tuple[bool, float]:
            metadata = memory.get("metadata") if isinstance(memory, dict) else None
            value = metadata.get(sort_by) if isinstance(metadata, dict) else None
            if value is None or value == "":
                return True, 0.0
            if sort_by == "relativity":
                try:
                    return False, float(value)
                except (TypeError, ValueError):
                    return True, 0.0
            parsed = parse_datetime(value)
            if parsed is None:
                return True, 0.0
            return False, parsed.timestamp()

        for groups in results.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                memories = group.get("memories") if isinstance(group, dict) else None
                if not isinstance(memories, list):
                    continue
                present = [mem for mem in memories if not sort_key(mem)[0]]
                missing = [mem for mem in memories if sort_key(mem)[0]]
                present.sort(key=sort_key, reverse=(sort_order == "desc"))
                group["memories"] = present + missing
        return results

    @staticmethod
    def _apply_relativity_threshold(results: dict[str, Any], relativity: float) -> dict[str, Any]:
        if relativity <= 0:
            return results

        for key in ("text_mem", "pref_mem", "event_mem"):
            buckets = results.get(key)
            if not isinstance(buckets, list):
                continue

            for bucket in buckets:
                memories = bucket.get("memories")
                if not isinstance(memories, list):
                    continue

                filtered: list[dict[str, Any]] = []
                for mem in memories:
                    if not isinstance(mem, dict):
                        continue
                    meta = mem.get("metadata", {})
                    score = meta.get("relativity", 1.0) if isinstance(meta, dict) else 1.0
                    try:
                        score_val = float(score) if score is not None else 1.0
                    except (TypeError, ValueError):
                        score_val = 1.0
                    if score_val >= relativity:
                        filtered.append(mem)

                bucket["memories"] = filtered
                if "total_nodes" in bucket:
                    bucket["total_nodes"] = len(filtered)

        return results

    def _dedup_text_memories(self, results: dict[str, Any], target_top_k: int) -> dict[str, Any]:
        buckets = results.get("text_mem", [])
        if not buckets:
            return results

        flat: list[tuple[int, dict[str, Any], float]] = []
        for bucket_idx, bucket in enumerate(buckets):
            for mem in bucket.get("memories", []):
                score = mem.get("metadata", {}).get("relativity", 0.0)
                flat.append((bucket_idx, mem, score))

        if len(flat) <= 1:
            return results

        embeddings = self._extract_embeddings([mem for _, mem, _ in flat])

        similarity_matrix = cosine_similarity_matrix(embeddings)

        indices_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(buckets))}
        for flat_index, (bucket_idx, _, _) in enumerate(flat):
            indices_by_bucket[bucket_idx].append(flat_index)

        selected_global: list[int] = []
        selected_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(buckets))}

        ordered_indices = sorted(range(len(flat)), key=lambda idx: flat[idx][2], reverse=True)
        for idx in ordered_indices:
            bucket_idx = flat[idx][0]
            if len(selected_by_bucket[bucket_idx]) >= target_top_k:
                continue
            # Use 0.92 threshold strictly
            if self._is_unrelated(idx, selected_global, similarity_matrix, 0.92):
                selected_by_bucket[bucket_idx].append(idx)
                selected_global.append(idx)

        # Removed the 'filling' logic that was pulling back similar items.
        # Now it will only return items that truly pass the 0.92 threshold,
        # up to target_top_k.

        for bucket_idx, bucket in enumerate(buckets):
            selected_indices = selected_by_bucket.get(bucket_idx, [])
            bucket["memories"] = [flat[i][1] for i in selected_indices]
        return results

    def _mmr_dedup_text_memories(
        self, results: dict[str, Any], text_top_k: int, pref_top_k: int = 6
    ) -> dict[str, Any]:
        """
        MMR-based deduplication with progressive penalty for high similarity.

        Performs deduplication on both text_mem and preference memories together.
        Other memory types (tool_mem, etc.) are not modified.

        Args:
            results: Search results containing text_mem and preference buckets
            text_top_k: Target number of text memories to return per bucket
            pref_top_k: Target number of preference memories to return per bucket

        Algorithm:
        1. Prefill top 5 by relevance
        2. MMR selection: balance relevance vs diversity
        3. Re-sort by original relevance for better generation quality
        """
        text_buckets = results.get("text_mem", [])
        pref_buckets = results.get("pref_mem", [])

        # Early return if no memories to deduplicate
        if not text_buckets and not pref_buckets:
            return results

        self._prune_mmr_candidates_by_bucket(
            results,
            text_top_k=text_top_k,
            pref_top_k=pref_top_k,
        )

        # Flatten all memories with their type and scores
        # flat structure: (memory_type, bucket_idx, mem, score)
        flat: list[tuple[str, int, dict[str, Any], float]] = []

        # Flatten text memories
        for bucket_idx, bucket in enumerate(text_buckets):
            for mem in bucket.get("memories", []):
                score = mem.get("metadata", {}).get("relativity", 0.0)
                flat.append(("text", bucket_idx, mem, float(score) if score is not None else 0.0))

        # Flatten preference memories
        for bucket_idx, bucket in enumerate(pref_buckets):
            for mem in bucket.get("memories", []):
                meta = mem.get("metadata", {})
                if isinstance(meta, dict):
                    score = meta.get("score", meta.get("relativity", 0.0))
                else:
                    score = 0.0
                flat.append(
                    ("preference", bucket_idx, mem, float(score) if score is not None else 0.0)
                )

        if len(flat) <= 1:
            return results

        total_by_type: dict[str, int] = {"text": 0, "preference": 0}
        existing_by_type: dict[str, int] = {"text": 0, "preference": 0}
        missing_by_type: dict[str, int] = {"text": 0, "preference": 0}
        missing_indices: list[int] = []
        for idx, (mem_type, _, mem, _) in enumerate(flat):
            if mem_type not in total_by_type:
                total_by_type[mem_type] = 0
                existing_by_type[mem_type] = 0
                missing_by_type[mem_type] = 0
            total_by_type[mem_type] += 1

            embedding = mem.get("metadata", {}).get("embedding")
            if embedding:
                existing_by_type[mem_type] += 1
            else:
                missing_by_type[mem_type] += 1
                missing_indices.append(idx)

        self.logger.info(
            "[SearchHandler] MMR embedding metadata scan: total=%s total_by_type=%s existing_by_type=%s missing_by_type=%s",
            len(flat),
            total_by_type,
            existing_by_type,
            missing_by_type,
        )
        if missing_indices:
            self.logger.warning(
                "[SearchHandler] MMR embedding metadata missing; will compute missing embeddings: missing_total=%s",
                len(missing_indices),
            )

        # Get or compute embeddings
        embeddings = self._extract_embeddings([mem for _, _, mem, _ in flat])

        # Compute similarity matrix using NumPy-optimized method
        # Returns numpy array but compatible with list[i][j] indexing
        similarity_matrix = cosine_similarity_matrix(embeddings)

        # Initialize selection tracking for both text and preference
        text_indices_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(text_buckets))}
        pref_indices_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(pref_buckets))}

        for flat_index, (mem_type, bucket_idx, _, _) in enumerate(flat):
            if mem_type == "text":
                text_indices_by_bucket[bucket_idx].append(flat_index)
            elif mem_type == "preference":
                pref_indices_by_bucket[bucket_idx].append(flat_index)

        selected_global: list[int] = []
        text_selected_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(text_buckets))}
        pref_selected_by_bucket: dict[int, list[int]] = {i: [] for i in range(len(pref_buckets))}
        selected_texts: set[str] = set()  # Track exact text content to avoid duplicates

        # Phase 1: Prefill top N by relevance
        # Use the smaller of text_top_k and pref_top_k for prefill count
        prefill_top_n = min(2, text_top_k, pref_top_k) if pref_buckets else min(2, text_top_k)
        ordered_by_relevance = sorted(range(len(flat)), key=lambda idx: flat[idx][3], reverse=True)
        for idx in ordered_by_relevance[: len(flat)]:
            if len(selected_global) >= prefill_top_n:
                break
            mem_type, bucket_idx, mem, _ = flat[idx]

            # Skip if exact text already exists in selected set
            mem_text = mem.get("memory", "").strip()
            if mem_text in selected_texts:
                continue

            # Skip if highly similar (Dice + TF-IDF + 2-gram combined, with embedding filter)
            if SearchHandler._is_text_highly_similar_optimized(
                idx, mem_text, selected_global, similarity_matrix, flat, threshold=0.92
            ):
                continue

            # Check bucket capacity with correct top_k for each type
            if mem_type == "text" and len(text_selected_by_bucket[bucket_idx]) < text_top_k:
                selected_global.append(idx)
                text_selected_by_bucket[bucket_idx].append(idx)
                selected_texts.add(mem_text)
            elif mem_type == "preference" and len(pref_selected_by_bucket[bucket_idx]) < pref_top_k:
                selected_global.append(idx)
                pref_selected_by_bucket[bucket_idx].append(idx)
                selected_texts.add(mem_text)

        # Phase 2: MMR selection for remaining slots
        lambda_relevance = 0.8
        similarity_threshold = 0.9  # Start exponential penalty from 0.9 (lowered from 0.9)
        alpha_exponential = 10.0  # Exponential penalty coefficient
        remaining = set(range(len(flat))) - set(selected_global)

        while remaining:
            best_idx: int | None = None
            best_mmr: float | None = None

            for idx in remaining:
                mem_type, bucket_idx, mem, _ = flat[idx]

                # Check bucket capacity with correct top_k for each type
                if (
                    mem_type == "text" and len(text_selected_by_bucket[bucket_idx]) >= text_top_k
                ) or (
                    mem_type == "preference"
                    and len(pref_selected_by_bucket[bucket_idx]) >= pref_top_k
                ):
                    continue

                # Check if exact text already exists - if so, skip this candidate entirely
                mem_text = mem.get("memory", "").strip()
                if mem_text in selected_texts:
                    continue  # Skip duplicate text, don't participate in MMR competition

                # Skip if highly similar (Dice + TF-IDF + 2-gram combined, with embedding filter)
                if SearchHandler._is_text_highly_similar_optimized(
                    idx, mem_text, selected_global, similarity_matrix, flat, threshold=0.92
                ):
                    continue  # Skip highly similar text, don't participate in MMR competition

                relevance = flat[idx][3]
                max_sim = (
                    0.0
                    if not selected_global
                    else max(similarity_matrix[idx][j] for j in selected_global)
                )

                # Exponential penalty for similarity > 0.80
                if max_sim > similarity_threshold:
                    penalty_multiplier = math.exp(
                        alpha_exponential * (max_sim - similarity_threshold)
                    )
                    diversity = max_sim * penalty_multiplier
                else:
                    diversity = max_sim

                mmr_score = lambda_relevance * relevance - (1.0 - lambda_relevance) * diversity

                if best_mmr is None or mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_idx = idx

            if best_idx is None:
                break

            mem_type, bucket_idx, mem, _ = flat[best_idx]

            # Add to selected set and track text
            mem_text = mem.get("memory", "").strip()
            selected_global.append(best_idx)
            selected_texts.add(mem_text)

            if mem_type == "text":
                text_selected_by_bucket[bucket_idx].append(best_idx)
            elif mem_type == "preference":
                pref_selected_by_bucket[bucket_idx].append(best_idx)
            remaining.remove(best_idx)

            # Early termination: all buckets are full
            text_all_full = all(
                len(text_selected_by_bucket[b_idx]) >= min(text_top_k, len(bucket_indices))
                for b_idx, bucket_indices in text_indices_by_bucket.items()
            )
            pref_all_full = all(
                len(pref_selected_by_bucket[b_idx]) >= min(pref_top_k, len(bucket_indices))
                for b_idx, bucket_indices in pref_indices_by_bucket.items()
            )
            if text_all_full and pref_all_full:
                break

        # Phase 3: Re-sort by original relevance and fill back to buckets
        for bucket_idx, bucket in enumerate(text_buckets):
            selected_indices = text_selected_by_bucket.get(bucket_idx, [])
            selected_indices = sorted(selected_indices, key=lambda i: flat[i][3], reverse=True)
            bucket["memories"] = [flat[i][2] for i in selected_indices]

        for bucket_idx, bucket in enumerate(pref_buckets):
            selected_indices = pref_selected_by_bucket.get(bucket_idx, [])
            selected_indices = sorted(selected_indices, key=lambda i: flat[i][3], reverse=True)
            bucket["memories"] = [flat[i][2] for i in selected_indices]

        return results

    def _prune_mmr_candidates_by_bucket(
        self,
        results: dict[str, Any],
        *,
        text_top_k: int,
        pref_top_k: int,
    ) -> dict[str, Any]:
        """Keep at most twice the final quota for each memory type in each bucket."""
        if not _env_enabled(_ENV_MMR_CANDIDATE_PRUNING, "off"):
            return results

        total_before = 0
        total_after = 0
        bucket_counts: list[tuple[int, int]] = []

        for result_key, target_top_k in (
            ("text_mem", text_top_k),
            ("pref_mem", pref_top_k),
        ):
            buckets = results.get(result_key)
            if not isinstance(buckets, list):
                continue

            candidate_limit = max(0, int(target_top_k)) * _MMR_CANDIDATE_MULTIPLIER
            for bucket in buckets:
                memories = bucket.get("memories") if isinstance(bucket, dict) else None
                if not isinstance(memories, list):
                    continue

                before_count = len(memories)
                total_before += before_count
                memories_by_type: dict[str, list[dict[str, Any]]] = {}
                for memory in memories:
                    if not isinstance(memory, dict):
                        continue
                    metadata = memory.get("metadata")
                    memory_type = (
                        metadata.get("memory_type") if isinstance(metadata, dict) else None
                    )
                    memories_by_type.setdefault(str(memory_type or result_key), []).append(memory)

                selected: list[dict[str, Any]] = []
                if candidate_limit > 0:
                    for typed_memories in memories_by_type.values():
                        selected.extend(
                            sorted(
                                typed_memories,
                                key=self._mmr_candidate_score,
                                reverse=True,
                            )[:candidate_limit]
                        )
                    selected.sort(key=self._mmr_candidate_score, reverse=True)

                bucket["memories"] = selected
                if "total_nodes" in bucket:
                    bucket["total_nodes"] = len(selected)
                total_after += len(selected)
                bucket_counts.append((before_count, len(selected)))

        self.logger.info(
            "[SearchHandler] MMR candidate pruning: multiplier=%s before=%s after=%s "
            "dropped=%s bucket_counts=%s",
            _MMR_CANDIDATE_MULTIPLIER,
            total_before,
            total_after,
            total_before - total_after,
            bucket_counts,
        )
        return results

    @staticmethod
    def _mmr_candidate_score(memory: dict[str, Any]) -> float:
        metadata = memory.get("metadata")
        if not isinstance(metadata, dict):
            return 0.0
        score = metadata.get("score", metadata.get("relativity", 0.0))
        try:
            return float(score or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_unrelated(
        index: int,
        selected_indices: list[int],
        similarity_matrix: list[list[float]],
        similarity_threshold: float,
    ) -> bool:
        return all(similarity_matrix[index][j] <= similarity_threshold for j in selected_indices)

    def _extract_embeddings(self, memories: list[dict[str, Any]]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        missing_indices: list[int] = []
        missing_documents: list[str] = []

        for idx, mem in enumerate(memories):
            metadata = mem.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                mem["metadata"] = metadata

            embedding = metadata.get("embedding")
            if embedding:
                embeddings.append(embedding)
                continue

            embeddings.append([])
            missing_indices.append(idx)
            missing_documents.append(mem.get("memory", ""))

        if missing_indices:
            # 结果对象里没带 embedding 的，先去向量库取；取不到再算。
            ids = [self._memory_id(memories[idx]) for idx in missing_indices]
            cached = self._vectors_from_store([i for i in ids if i])
            pending: list[int] = []
            pending_docs: list[str] = []
            for idx, mem_id in zip(missing_indices, ids, strict=False):
                vector = cached.get(mem_id) if mem_id else None
                if vector:
                    embeddings[idx] = vector
                    memories[idx]["metadata"]["embedding"] = vector
                else:
                    pending.append(idx)
                    pending_docs.append(memories[idx].get("memory", ""))

            if pending:
                computed = self.searcher.embedder.embed(pending_docs)
                for idx, embedding in zip(pending, computed, strict=False):
                    embeddings[idx] = embedding
                    memories[idx]["metadata"]["embedding"] = embedding

            self.logger.info(
                "[SearchHandler] embedding for dedup: from_store=%s recomputed=%s",
                len(missing_indices) - len(pending),
                len(pending),
            )

        return embeddings

    @staticmethod
    def _memory_id(mem: dict[str, Any]) -> str:
        metadata = mem.get("metadata")
        if isinstance(metadata, dict) and metadata.get("id"):
            return str(metadata["id"])
        return str(mem.get("id") or "")

    def _vectors_from_store(self, ids: list[str]) -> dict[str, list[float]]:
        """从向量库按 id 批量读向量（只读，取不到就退回重新 embed）。"""
        if not ids:
            return {}
        try:
            text_mem = getattr(getattr(self, "naive_mem_cube", None), "text_mem", None)
            store = getattr(text_mem, "graph_store", None)
            vec_db = getattr(store, "vec_db", None)
            if vec_db is None:
                return {}
            items = vec_db.get_by_ids(ids) or []
        except Exception:
            self.logger.debug("读取向量库失败，退回重新计算 embedding", exc_info=True)
            return {}
        vectors: dict[str, list[float]] = {}
        for item in items:
            item_id = getattr(item, "id", None)
            vector = getattr(item, "vector", None)
            if item_id and vector:
                vectors[str(item_id)] = vector
        return vectors

    @staticmethod
    def _strip_embeddings(results: dict[str, Any]) -> None:
        for _mem_type, mem_results in results.items():
            if isinstance(mem_results, list):
                for bucket in mem_results:
                    for mem in bucket.get("memories", []):
                        metadata = mem.get("metadata", {})
                        if "embedding" in metadata:
                            metadata["embedding"] = []

    @staticmethod
    def _dice_similarity(text1: str, text2: str) -> float:
        """
        Calculate Dice coefficient (character-level, fastest).

        Dice = 2 * |A ∩ B| / (|A| + |B|)
        Speed: O(n + m), ~0.05-0.1ms per comparison

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Dice similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        chars1 = set(text1)
        chars2 = set(text2)

        intersection = len(chars1 & chars2)
        return 2 * intersection / (len(chars1) + len(chars2))

    @staticmethod
    def _bigram_similarity(text1: str, text2: str) -> float:
        """
        Calculate character-level 2-gram Jaccard similarity.

        Speed: O(n + m), ~0.1-0.2ms per comparison
        Considers local order (more strict than Dice).

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Jaccard similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        # Generate 2-grams
        bigrams1 = {text1[i : i + 2] for i in range(len(text1) - 1)} if len(text1) >= 2 else {text1}
        bigrams2 = {text2[i : i + 2] for i in range(len(text2) - 1)} if len(text2) >= 2 else {text2}

        intersection = len(bigrams1 & bigrams2)
        union = len(bigrams1 | bigrams2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _tfidf_similarity(text1: str, text2: str) -> float:
        """
        Calculate TF-IDF cosine similarity (character-level, no sklearn).

        Speed: O(n + m), ~0.3-0.5ms per comparison
        Considers character frequency weighting.

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Cosine similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        from collections import Counter

        # Character frequency (TF)
        tf1 = Counter(text1)
        tf2 = Counter(text2)

        # All unique characters (vocabulary)
        vocab = set(tf1.keys()) | set(tf2.keys())

        # Simple IDF: log(2 / df) where df is document frequency
        # For two documents, IDF is log(2/1)=0.693 if char appears in one doc,
        # or log(2/2)=0 if appears in both (we use log(2/1) for simplicity)
        idf = {char: (1.0 if char in tf1 and char in tf2 else 1.5) for char in vocab}

        # TF-IDF vectors
        vec1 = {char: tf1.get(char, 0) * idf[char] for char in vocab}
        vec2 = {char: tf2.get(char, 0) * idf[char] for char in vocab}

        # Cosine similarity
        dot_product = sum(vec1[char] * vec2[char] for char in vocab)
        norm1 = math.sqrt(sum(v * v for v in vec1.values()))
        norm2 = math.sqrt(sum(v * v for v in vec2.values()))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    @staticmethod
    def _is_text_highly_similar_optimized(
        candidate_idx: int,
        candidate_text: str,
        selected_global: list[int],
        similarity_matrix,
        flat: list,
        threshold: float = 0.9,
    ) -> bool:
        """
        Multi-algorithm text similarity check with embedding pre-filtering.

        Strategy:
        1. Only compare with the single highest embedding similarity item (not all 25)
        2. Only perform text comparison if embedding similarity > 0.60
        3. Use weighted combination of three algorithms:
           - Dice (40%): Fastest, character-level set similarity
           - TF-IDF (35%): Considers character frequency weighting
           - 2-gram (25%): Considers local character order

        Combined formula:
            combined_score = 0.40 * dice + 0.35 * tfidf + 0.25 * bigram

        This reduces comparisons from O(N) to O(1) per candidate, with embedding pre-filtering.
        Expected speedup: 100-200x compared to LCS approach.

        Args:
            candidate_idx: Index of candidate memory in flat list
            candidate_text: Text content of candidate memory
            selected_global: List of already selected memory indices
            similarity_matrix: Precomputed embedding similarity matrix
            flat: Flat list of all memories
            threshold: Combined similarity threshold (default 0.75)

        Returns:
            True if candidate is highly similar to any selected memory
        """
        if not selected_global:
            return False

        # Find the already-selected memory with highest embedding similarity
        max_sim_idx = max(selected_global, key=lambda j: similarity_matrix[candidate_idx][j])
        max_sim = similarity_matrix[candidate_idx][max_sim_idx]

        # If highest embedding similarity < 0.60, skip text comparison entirely
        if max_sim <= 0.9:
            return False

        # Get text of most similar memory
        most_similar_mem = flat[max_sim_idx][2]
        most_similar_text = most_similar_mem.get("memory", "").strip()

        # Calculate three similarity scores
        dice_sim = SearchHandler._dice_similarity(candidate_text, most_similar_text)
        tfidf_sim = SearchHandler._tfidf_similarity(candidate_text, most_similar_text)
        bigram_sim = SearchHandler._bigram_similarity(candidate_text, most_similar_text)

        # Weighted combination: Dice (40%) + TF-IDF (35%) + 2-gram (25%)
        # Dice has highest weight (fastest and most reliable)
        # TF-IDF considers frequency (handles repeated characters well)
        # 2-gram considers order (catches local pattern similarity)
        combined_score = 0.40 * dice_sim + 0.35 * tfidf_sim + 0.25 * bigram_sim

        return combined_score >= threshold

    @staticmethod
    def _dice_similarity(text1: str, text2: str) -> float:
        """
        Calculate Dice coefficient (character-level, fastest).

        Dice = 2 * |A ∩ B| / (|A| + |B|)
        Speed: O(n + m), ~0.05-0.1ms per comparison

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Dice similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        chars1 = set(text1)
        chars2 = set(text2)

        intersection = len(chars1 & chars2)
        return 2 * intersection / (len(chars1) + len(chars2))

    @staticmethod
    def _bigram_similarity(text1: str, text2: str) -> float:
        """
        Calculate character-level 2-gram Jaccard similarity.

        Speed: O(n + m), ~0.1-0.2ms per comparison
        Considers local order (more strict than Dice).

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Jaccard similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        # Generate 2-grams
        bigrams1 = {text1[i : i + 2] for i in range(len(text1) - 1)} if len(text1) >= 2 else {text1}
        bigrams2 = {text2[i : i + 2] for i in range(len(text2) - 1)} if len(text2) >= 2 else {text2}

        intersection = len(bigrams1 & bigrams2)
        union = len(bigrams1 | bigrams2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _tfidf_similarity(text1: str, text2: str) -> float:
        """
        Calculate TF-IDF cosine similarity (character-level, no sklearn).

        Speed: O(n + m), ~0.3-0.5ms per comparison
        Considers character frequency weighting.

        Args:
            text1: First text string
            text2: Second text string

        Returns:
            Cosine similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        from collections import Counter

        # Character frequency (TF)
        tf1 = Counter(text1)
        tf2 = Counter(text2)

        # All unique characters (vocabulary)
        vocab = set(tf1.keys()) | set(tf2.keys())

        # Simple IDF: log(2 / df) where df is document frequency
        # For two documents, IDF is log(2/1)=0.693 if char appears in one doc,
        # or log(2/2)=0 if appears in both (we use log(2/1) for simplicity)
        idf = {char: (1.0 if char in tf1 and char in tf2 else 1.5) for char in vocab}

        # TF-IDF vectors
        vec1 = {char: tf1.get(char, 0) * idf[char] for char in vocab}
        vec2 = {char: tf2.get(char, 0) * idf[char] for char in vocab}

        # Cosine similarity
        dot_product = sum(vec1[char] * vec2[char] for char in vocab)
        norm1 = math.sqrt(sum(v * v for v in vec1.values()))
        norm2 = math.sqrt(sum(v * v for v in vec2.values()))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    @staticmethod
    def _is_text_highly_similar_optimized(
        candidate_idx: int,
        candidate_text: str,
        selected_global: list[int],
        similarity_matrix,
        flat: list,
        threshold: float = 0.92,
    ) -> bool:
        """
        Multi-algorithm text similarity check with embedding pre-filtering.

        Strategy:
        1. Only compare with the single highest embedding similarity item (not all 25)
        2. Only perform text comparison if embedding similarity > 0.60
        3. Use weighted combination of three algorithms:
           - Dice (40%): Fastest, character-level set similarity
           - TF-IDF (35%): Considers character frequency weighting
           - 2-gram (25%): Considers local character order

        Combined formula:
            combined_score = 0.40 * dice + 0.35 * tfidf + 0.25 * bigram

        This reduces comparisons from O(N) to O(1) per candidate, with embedding pre-filtering.
        Expected speedup: 100-200x compared to LCS approach.

        Args:
            candidate_idx: Index of candidate memory in flat list
            candidate_text: Text content of candidate memory
            selected_global: List of already selected memory indices
            similarity_matrix: Precomputed embedding similarity matrix
            flat: Flat list of all memories
            threshold: Combined similarity threshold (default 0.75)

        Returns:
            True if candidate is highly similar to any selected memory
        """
        if not selected_global:
            return False

        # Find the already-selected memory with highest embedding similarity
        max_sim_idx = max(selected_global, key=lambda j: similarity_matrix[candidate_idx][j])
        max_sim = similarity_matrix[candidate_idx][max_sim_idx]

        # If highest embedding similarity < 0.60, skip text comparison entirely
        if max_sim <= 0.9:
            return False

        # Get text of most similar memory
        most_similar_mem = flat[max_sim_idx][2]
        most_similar_text = most_similar_mem.get("memory", "").strip()

        # Calculate three similarity scores
        dice_sim = SearchHandler._dice_similarity(candidate_text, most_similar_text)
        tfidf_sim = SearchHandler._tfidf_similarity(candidate_text, most_similar_text)
        bigram_sim = SearchHandler._bigram_similarity(candidate_text, most_similar_text)

        # Weighted combination: Dice (40%) + TF-IDF (35%) + 2-gram (25%)
        # Dice has highest weight (fastest and most reliable)
        # TF-IDF considers frequency (handles repeated characters well)
        # 2-gram considers order (catches local pattern similarity)
        combined_score = 0.40 * dice_sim + 0.35 * tfidf_sim + 0.25 * bigram_sim

        return combined_score >= threshold

    def _resolve_cube_ids(self, search_req: APISearchRequest) -> list[str]:
        """
        Normalize target cube ids from search_req.
        Priority:
        1) readable_cube_ids (deprecated mem_cube_id is converted to this in model validator)
        2) fallback to user_id
        """
        if search_req.readable_cube_ids:
            return list(dict.fromkeys(search_req.readable_cube_ids))

        return [search_req.user_id]

    def _build_cube_view(self, search_req: APISearchRequest, searcher=None) -> MemCubeView:
        cube_ids = self._resolve_cube_ids(search_req)
        searcher_to_use = searcher if searcher is not None else self.searcher

        if len(cube_ids) == 1:
            cube_id = cube_ids[0]
            return SingleCubeView(
                cube_id=cube_id,
                naive_mem_cube=self.naive_mem_cube,
                mem_reader=self.mem_reader,
                mem_scheduler=self.mem_scheduler,
                logger=self.logger,
                searcher=searcher_to_use,
                deepsearch_agent=self.deepsearch_agent,
            )
        else:
            single_views = [
                SingleCubeView(
                    cube_id=cube_id,
                    naive_mem_cube=self.naive_mem_cube,
                    mem_reader=self.mem_reader,
                    mem_scheduler=self.mem_scheduler,
                    logger=self.logger,
                    searcher=searcher_to_use,
                    deepsearch_agent=self.deepsearch_agent,
                )
                for cube_id in cube_ids
            ]
            return CompositeCubeView(cube_views=single_views, logger=self.logger)
