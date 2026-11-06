import re

from memos.embedders.base import BaseEmbedder
from memos.graph_dbs.base import BaseGraphDB
from memos.llms.base import BaseLLM
from memos.log import get_logger, preview_text
from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize import versioning
from memos.templates.tree_reorganize_prompts import (
    MEMORY_RELATION_DETECTOR_PROMPT,
    MEMORY_RELATION_RESOLVER_PROMPT,
)
from memos.time_basis import convert_legacy_node
from memos.utils import normalize_to_utc_iso, parse_datetime, utc_now_iso


logger = get_logger(__name__)

# 融合结果的长度上限。LLM 的输出长度不受控，超过这个量级的不可能是"一条记忆"。
MAX_RESOLVED_MEMORY_CHARS = 8192

# 关系标签。旧版 detector 的 contradictory 等价于 supersedes（"较晚的一句取代较早的一句"）。
RELATIONS = ("supersedes", "redundant", "both_valid", "independent")
LEGACY_RELATIONS = {"contradictory": "supersedes"}

# MERGED_TO 链的最大深度，超过就当作有环处理，宁可不加边。
MAX_MERGE_CHAIN_DEPTH = 64


def parse_relation(raw: str) -> str:
    """LLM 回答 -> 关系标签。整条回答必须就是一个标签（允许引号、标点和空白），其余一律当
    independent（不动数据）。不在句子里找标签词：「not redundant」「cannot tell whether A
    supersedes B」这类回答里含有标签词，但意思相反或没有结论。
    """
    word = re.sub(r"[\s\"'`*.,;:!()\[\]{}<>]+", "", (raw or "").lower())
    word = LEGACY_RELATIONS.get(word, word)
    return word if word in RELATIONS else "independent"


def _is_preference(item) -> bool:
    meta = getattr(item, "metadata", None)
    pref = getattr(meta, "preference", None)
    return getattr(meta, "memory_type", None) == "PreferenceMemory" or (
        isinstance(pref, str) and bool(pref.strip())
    )


def _relation_text(item) -> str:
    """判重/融合用文本：偏好类记忆取 metadata.preference（抽象偏好本身），其余取 memory 叙述。

    原实现一律用 memory 叙述（含 URL/时间戳等任务实例细节），导致"同一偏好、不同任务实例"
    被 LLM 判为 independent，去重失效——同一偏好可堆积上百份副本。
    """
    pref = getattr(getattr(item, "metadata", None), "preference", None)
    if isinstance(pref, str) and pref.strip():
        return pref.strip()
    return item.memory


class NodeHandler:
    EMBEDDING_THRESHOLD: float = 0.8  # Threshold for embedding similarity to consider conflict

    def __init__(self, graph_store: BaseGraphDB, llm: BaseLLM, embedder: BaseEmbedder):
        self.graph_store = graph_store
        self.llm = llm
        self.embedder = embedder

    def detect(self, memory, top_k: int = 5, scope=None, user_name: str | None = None):
        """找出与 ``memory`` 冲突或重复的已有节点，返回 ``[memory, 候选, 关系]`` 列表。

        关系只会是 ``supersedes``（较晚的一句取代较早的一句）或 ``redundant``。事件形态
        （一次性发生的事，如两次出差）不是可被取代的状态，整对跳过，不调 LLM。
        """
        if versioning.is_event(memory):
            return []
        # 1. Search for similar memories based on embedding
        embedding = memory.metadata.embedding
        embedding_candidates_info = self.graph_store.search_by_embedding(
            embedding,
            top_k=top_k,
            scope=scope,
            # 只拿 activated 作候选：归档节点（含已被合并掉的、以及"出生即归档"的合并产物）
            # 若仍参与判重，会把新写入的记忆再并进一个归档节点，形成链式重合并。
            status="activated",
            threshold=self.EMBEDDING_THRESHOLD,
            user_name=user_name,
        )
        # 2. Filter based on similarity threshold
        embedding_candidates_ids = [
            info["id"] for info in embedding_candidates_info if info["id"] != memory.id
        ]
        # 3. Judge each pair with the LLM
        embedding_candidates = self.graph_store.get_nodes(
            embedding_candidates_ids, user_name=user_name
        )
        detected_relationships = []
        for embedding_candidate in embedding_candidates:
            try:
                candidate = TextualMemoryItem.from_dict(embedding_candidate)
            except Exception as e:  # noqa: BLE001 - one unreadable node must not stop dedup
                logger.warning(f"Skip unreadable conflict candidate: {e}")
                continue
            if versioning.is_event(candidate):
                continue
            # 代码按时间把较早的一句放 A、较晚的放 B；LLM 只判语义，不判先后。
            older, newer = versioning.order_pair(memory, candidate)
            prompt = [
                {
                    "role": "user",
                    "content": MEMORY_RELATION_DETECTOR_PROMPT.format(
                        statement_1=_relation_text(older),
                        time_1=versioning.display_time(older),
                        statement_2=_relation_text(newer),
                        time_2=versioning.display_time(newer),
                    ),
                }
            ]
            relation = parse_relation(self.llm.generate(prompt))
            if relation in ("supersedes", "redundant"):
                logger.info(
                    f'detected "{_relation_text(memory)}" <=={relation.upper()}==> '
                    f'"{_relation_text(candidate)}"'
                )
                detected_relationships.append([memory, candidate, relation])
        return detected_relationships

    def resolve(
        self,
        memory_a: TextualMemoryItem,
        memory_b: TextualMemoryItem,
        relation,
        user_name: str | None = None,
    ) -> str | None:
        """处理 detect 找到的一对冲突/重复记忆。

        Args:
            memory_a: 本次新写入的节点。
            memory_b: 已有节点。
            relation: ``supersedes`` / ``redundant``（旧值 ``contradictory`` 等价于 supersedes）。
            user_name: Optional user name for multi-tenant isolation.
        Returns:
            被并入对方、已归档的节点 id；什么都没做（节点已不在、已归档）时返回 None。

        两个节点谁是 head 由时间决定（``chat_time``，缺失退写入顺序），所以补录的旧对话
        不会覆盖更新的事实。``supersedes`` 不调 LLM：head 保持原文，较旧的一句进它的
        ``history``。``redundant`` 以及偏好类记忆走 LLM 融合；融合不出来时只归档较旧的
        一方，不再删除。
        """
        relation = LEGACY_RELATIONS.get(relation, relation)
        # 同一个新节点可能连续命中多个旧节点；每次都从图里重读最新状态，
        # 否则后一次会拿陈旧对象覆盖前一次写进去的 history。
        fresh_a = self._fresh(memory_a, user_name)
        fresh_b = self._fresh(memory_b, user_name)
        if fresh_a is None or fresh_b is None:
            return None
        older, newer = versioning.order_pair(fresh_a, fresh_b)
        if relation == "supersedes" and not (_is_preference(older) or _is_preference(newer)):
            self._supersede(newer, older, user_name=user_name)
        else:
            fusion = "contradictory" if relation == "supersedes" else "redundant"
            self._fuse(newer, older, fusion, user_name=user_name)
        return older.id

    def _fresh(self, item: TextualMemoryItem, user_name: str | None) -> TextualMemoryItem | None:
        """重读节点当前状态。节点已不在或已不是 activated 返回 None；读不到就用传入的对象。"""
        try:
            node = self.graph_store.get_node(item.id, user_name=user_name)
        except Exception as e:  # noqa: BLE001 - best effort, fall back to the given copy
            logger.warning(f"Reload of {item.id} failed, using the given copy: {e}")
            return item
        if node is None:
            return None
        if not isinstance(node, dict):
            return item
        try:
            fresh = TextualMemoryItem.from_dict(node)
        except Exception as e:  # noqa: BLE001 - best effort, fall back to the given copy
            logger.warning(f"Reloaded {item.id} is unreadable, using the given copy: {e}")
            return item
        if fresh.metadata.status != "activated":
            return None
        if getattr(item.metadata, "embedding", None) is not None:
            fresh.metadata.embedding = item.metadata.embedding
        return fresh

    def _supersede(self, head, tail, user_name: str | None = None) -> None:
        """版本链：``head`` 保持原文，``tail`` 的文本进 head 的 history，tail 归档。"""
        fields = versioning.merge_into_head(head, tail)
        self.graph_store.update_node(head.id, fields, user_name=user_name)
        self._archive_loser(
            head, tail, user_name=user_name, extra={"valid_to": fields["valid_from"]}
        )
        logger.info(f"Superseded {tail.id} by {head.id}; version={fields['version']}")

    def _fuse(self, keeper, loser, relation: str, user_name: str | None = None) -> None:
        """LLM 融合两句话写回 keeper；融合不出来就只归档 loser，不删除任何数据。"""
        metadata_for_resolve = ["key", "background", "confidence", "updated_at", "chat_time"]
        metadata_1 = keeper.metadata.model_dump_json(include=metadata_for_resolve)
        metadata_2 = loser.metadata.model_dump_json(include=metadata_for_resolve)
        prompt = [
            {
                "role": "user",
                "content": MEMORY_RELATION_RESOLVER_PROMPT.format(
                    relation=relation,
                    statement_1=_relation_text(keeper),
                    metadata_1=metadata_1,
                    statement_2=_relation_text(loser),
                    metadata_2=metadata_2,
                ),
            },
        ]
        try:
            response = self.llm.generate(prompt).strip()
        except Exception as e:  # noqa: BLE001 - an LLM failure means "cannot fuse"
            logger.warning(f"Fusion LLM call failed for {keeper.id} / {loser.id}: {e}")
            response = ""
        matched = re.search(r"<answer>(.*?)</answer>", response, re.DOTALL)
        answer = matched.group(1).strip() if matched else ""

        # 三种情况都按"解不出来"处理：
        #   ① 没有 <answer> 标签；
        #   ② 回答为空——否则会拿空串当融合结果覆盖 keeper，把正文清掉；
        #   ③ 回答长到不可能是"一条记忆"。实测有一次 LLM 返回了约 173 MB 的反斜杠，
        #      一条日志记录就是 173 MB。上限 8192 字符，正常融合结果是几十到几百字符。
        # 原实现此时 delete_node 删掉较旧的一方，不可逆；现在只归档它（文本保留在归档节点上）。
        if (
            not answer
            or len(answer) > MAX_RESOLVED_MEMORY_CHARS
            or (len(answer) <= 10 and "no" in answer.lower())
        ):
            logger.warning(
                f"{relation} between {keeper.id} and {loser.id} could not be fused, "
                f"archiving {loser.id} without rewriting {keeper.id}. answer_chars={len(answer)}"
            )
            fields = versioning.merge_into_head(keeper, loser, update_type="duplicate")
            self.graph_store.update_node(keeper.id, fields, user_name=user_name)
            self._archive_loser(
                keeper, loser, user_name=user_name, extra={"valid_to": fields["valid_from"]}
            )
            return

        fixed_metadata = self._merge_metadata(answer, keeper.metadata, loser.metadata)
        merged_memory = TextualMemoryItem(memory=answer, metadata=fixed_metadata)
        logger.info(f"Resolved result: {preview_text(merged_memory)}")
        self._resolve_in_graph(keeper, loser, merged_memory, user_name=user_name)

    def _would_cycle(self, loser_id: str, keeper_id: str, user_name: str | None = None) -> bool:
        """加 ``loser -> keeper`` 的 MERGED_TO 边会不会成环：从 keeper 沿 MERGED_TO 能否走回 loser。"""
        if loser_id == keeper_id:
            return True
        seen = {keeper_id}
        frontier = [keeper_id]
        for _ in range(MAX_MERGE_CHAIN_DEPTH):
            following = []
            for node_id in frontier:
                for edge in self.graph_store.get_edges(
                    node_id, type="MERGED_TO", direction="OUTGOING", user_name=user_name
                ):
                    if edge.get("type") != "MERGED_TO" or edge.get("from") != node_id:
                        continue
                    target = edge.get("to")
                    if target == loser_id:
                        return True
                    if target and target not in seen:
                        seen.add(target)
                        following.append(target)
            if not following:
                return False
            frontier = following
        return True

    def _archive_loser(
        self,
        keeper: TextualMemoryItem,
        loser: TextualMemoryItem,
        user_name: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """把 loser 并入 keeper：继承它的非 MERGED_TO 边，归档，再补一条 MERGED_TO。"""
        # 只继承非 MERGED_TO 的边（如 PARENT 层级），避免合并关系自我繁殖。
        for edge in self.graph_store.get_edges(
            loser.id, type="ANY", direction="ANY", user_name=user_name
        ):
            if edge["type"] == "MERGED_TO":
                continue
            new_from = keeper.id if edge["from"] == loser.id else edge["from"]
            new_to = keeper.id if edge["to"] == loser.id else edge["to"]
            if new_from == new_to:
                continue
            if not self.graph_store.edge_exists(
                new_from, new_to, edge["type"], direction="ANY", user_name=user_name
            ):
                self.graph_store.add_edge(new_from, new_to, edge["type"], user_name=user_name)

        archived_fields = {"status": "archived", **(extra or {})}
        self.graph_store.update_node(loser.id, archived_fields, user_name=user_name)
        # 检索侧按 Qdrant payload.status 过滤。update_node 已经会同步 payload，这里再直接写一次，
        # 是为了不依赖具体图库实现——否则刚归档的节点会继续"幽灵激活"进入召回（实测活跃租户
        # 每天累积约 500 条，与存量 keeper 同量级）。每日巡检 memos-status-sync 只是兜底。
        _vec_db = getattr(self.graph_store, "vec_db", None)
        if _vec_db is not None:
            try:
                _vec_db.update(
                    loser.id,
                    {"id": loser.id, "vector": None, "payload": archived_fields},
                )
            except Exception as e:  # noqa: BLE001 - a vector-store hiccup must not undo the merge
                logger.warning(f"[VecDB] status sync failed for {loser.id}: {e}")
        if self._would_cycle(loser.id, keeper.id, user_name=user_name):
            logger.warning(f"Skip MERGED_TO {loser.id} -> {keeper.id}: it would close a cycle")
            return
        self.graph_store.add_edge(loser.id, keeper.id, type="MERGED_TO", user_name=user_name)
        logger.debug(f"Merged {loser.id} into {keeper.id} in place.")

    def _resolve_in_graph(
        self,
        conflict_a: TextualMemoryItem,
        conflict_b: TextualMemoryItem,
        merged: TextualMemoryItem,
        user_name: str | None = None,
    ):
        # 就地合并：把 keeper（conflict_a，时间上较新的一方）原地升级为融合结果，只归档
        # 较旧的一方（conflict_b）。
        #
        # 原实现每次合并都新建一个节点，并把双方的边全部复制到新节点上。这带来两个后果：
        #   ① 同一批里多条并行合并会各自产出"兄弟"新节点，同一偏好永远收敛不到一条；
        #   ② 复制 MERGED_TO 边让合并关系变成多叉并成环（实测 8,974 条偏好产出 1,898 个
        #      合并产物、37,566 条 MERGED_TO、56 个环、最大环 862 节点）。
        # add_node 用的是 MERGE，传同一个 id 就是原地更新（neo4j 与 qdrant 一并覆盖）。
        keeper, loser = conflict_a, conflict_b

        self.graph_store.add_node(
            keeper.id,
            merged.memory,
            merged.metadata.model_dump(exclude_none=True),
            user_name=user_name,
        )
        self._archive_loser(keeper, loser, user_name=user_name)

    @staticmethod
    def _pick_time(first: str | None, second: str | None, *, earliest: bool) -> str | None:
        candidates = [(parse_datetime(normalize_to_utc_iso(v)), v) for v in (first, second) if v]
        candidates = [(dt, v) for dt, v in candidates if dt is not None]
        if not candidates:
            return first or second
        return (min if earliest else max)(candidates, key=lambda pair: pair[0])[1]

    def _merge_metadata(
        self,
        memory: str,
        metadata_a: TreeNodeTextualMemoryMetadata,
        metadata_b: TreeNodeTextualMemoryMetadata,
    ) -> TreeNodeTextualMemoryMetadata:
        metadata_1 = metadata_a.model_dump()
        metadata_2 = metadata_b.model_dump()
        # A merge picks the earliest `created_at` / latest `chat_time` of both
        # sides, so each side must be read in its own basis first; otherwise a
        # pre-T1.3 wall-clock value and a real-UTC one are compared as if they
        # were the same kind. After conversion the merged node is uniformly UTC.
        convert_legacy_node(metadata_1)
        convert_legacy_node(metadata_2)
        merged_metadata = {
            "sources": (metadata_1["sources"] or []) + (metadata_2["sources"] or []),
            "embedding": self.embedder.embed([memory])[0],
            "updated_at": utc_now_iso(),
            # 记忆最早是什么时候出现的；原实现这里重置成"现在"，且 updated_at 拼成了 update_at。
            "created_at": self._pick_time(
                metadata_1.get("created_at"), metadata_2.get("created_at"), earliest=True
            ),
        }
        chat_time = self._pick_time(
            metadata_1.get("chat_time"), metadata_2.get("chat_time"), earliest=False
        )
        if chat_time:
            merged_metadata["chat_time"] = chat_time
        # 注意：不要把 status 写死成 activated。就地合并时产物就是 keeper 本身，
        # 状态应当继承它自己的状态；调用方（handle_add）已保证参与合并的节点是 activated。
        for key in metadata_1:
            if key in merged_metadata:
                continue
            merged_metadata[key] = (
                metadata_1[key] if metadata_1[key] is not None else metadata_2[key]
            )
        return TreeNodeTextualMemoryMetadata.model_validate(merged_metadata)
