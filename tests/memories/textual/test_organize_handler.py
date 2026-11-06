"""Regression tests for the conflict-merge handler.

``_resolve_in_graph`` used to build a brand-new node per merge and copy both
sides' edges onto it. Two consequences:

* sibling merge products for the same preference, so it never converged to one
  row;
* copied ``MERGED_TO`` edges turned the merge relation into a cyclic multi-way
  graph.

It now merges in place: the newly written node becomes the fused row, the older
one is archived and pointed at it.
"""

import json
import uuid

from types import SimpleNamespace
from unittest.mock import MagicMock, call

from memos.memories.textual.item import TextualMemoryItem, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.handler import NodeHandler, _relation_text


def _item(item_id):
    item = MagicMock()
    item.id = item_id
    item.memory = f"memory of {item_id}"
    item.metadata.model_dump.return_value = {"sources": [], "embedding": [0.1]}
    return item


def _handler(graph_store=None):
    return NodeHandler(graph_store or MagicMock(), MagicMock(), MagicMock())


def _merge(handler, edges=None, edge_exists=False):
    graph_store = handler.graph_store
    graph_store.get_edges.return_value = edges or []
    graph_store.edge_exists.return_value = edge_exists
    keeper, loser, merged = _item("keeper"), _item("loser"), _item("merged")
    handler._resolve_in_graph(keeper, loser, merged, user_name="example-agent")
    return keeper, loser


# ─────────────────────────── _relation_text ───────────────────────────


def test_relation_text_prefers_the_abstract_preference():
    """The narrative carries task-instance detail (URLs, timestamps) that made the
    LLM call two instances of one preference independent."""
    item = SimpleNamespace(
        memory="2026-10-01 对 example.com 的扫描结论",
        metadata=SimpleNamespace(preference="  扫描结论需人工复核  "),
    )

    assert _relation_text(item) == "扫描结论需人工复核"


def test_relation_text_falls_back_to_the_narrative():
    item = SimpleNamespace(memory="narrative", metadata=SimpleNamespace(preference=None))

    assert _relation_text(item) == "narrative"


def test_relation_text_ignores_a_blank_preference():
    item = SimpleNamespace(memory="narrative", metadata=SimpleNamespace(preference="   "))

    assert _relation_text(item) == "narrative"


def test_relation_text_survives_missing_metadata():
    assert _relation_text(SimpleNamespace(memory="narrative")) == "narrative"


# ────────────────────────────── detect ──────────────────────────────


def test_detect_only_considers_activated_candidates():
    """Archived rows still in the vector store would otherwise be re-merged into,
    chaining one preference through a series of dead nodes."""
    graph_store = MagicMock()
    graph_store.search_by_embedding.return_value = []
    handler = _handler(graph_store)
    memory = MagicMock()
    memory.id = "n0"
    memory.metadata.embedding = [0.1, 0.2]

    handler.detect(memory, scope="LongTermMemory", user_name="example-agent")

    assert graph_store.search_by_embedding.call_args.kwargs["status"] == "activated"


# ──────────────────────── _resolve_in_graph ────────────────────────


def test_merge_updates_the_newly_written_node_in_place():
    """The regression: a third node was created for every merge."""
    handler = _handler()

    _merge(handler)

    assert handler.graph_store.add_node.call_args.args[0] == "keeper"


def test_merge_archives_only_the_loser():
    handler = _handler()

    _merge(handler)

    assert handler.graph_store.update_node.call_args_list == [
        call("loser", {"status": "archived"}, user_name="example-agent")
    ]


def test_merge_writes_a_single_merged_to_edge_pointing_at_the_keeper():
    handler = _handler()

    _merge(handler)

    assert handler.graph_store.add_edge.call_args_list == [
        call("loser", "keeper", type="MERGED_TO", user_name="example-agent")
    ]


def test_merge_does_not_inherit_merged_to_edges():
    """Copying them is what turned the merge relation cyclic."""
    handler = _handler()

    _merge(handler, edges=[{"from": "loser", "to": "elsewhere", "type": "MERGED_TO"}])

    assert handler.graph_store.add_edge.call_args_list == [
        call("loser", "keeper", type="MERGED_TO", user_name="example-agent")
    ]


def test_merge_inherits_edges_that_are_not_merged_to():
    handler = _handler()

    _merge(handler, edges=[{"from": "parent", "to": "loser", "type": "PARENT"}])

    assert call("parent", "keeper", "PARENT", user_name="example-agent") in (
        handler.graph_store.add_edge.call_args_list
    )


def test_merge_does_not_re_add_an_edge_that_already_exists():
    handler = _handler()

    _merge(handler, edges=[{"from": "parent", "to": "loser", "type": "PARENT"}], edge_exists=True)

    assert handler.graph_store.add_edge.call_args_list == [
        call("loser", "keeper", type="MERGED_TO", user_name="example-agent")
    ]


def test_merge_syncs_the_archived_status_to_the_vector_store():
    """update_node only touches Neo4j; retrieval filters on the Qdrant payload, so
    an unsynced archive keeps the row recallable as a ghost."""
    handler = _handler()
    handler.graph_store.vec_db = MagicMock()

    _merge(handler)

    handler.graph_store.vec_db.update.assert_called_once_with(
        "loser", {"id": "loser", "vector": None, "payload": {"status": "archived"}}
    )


def test_merge_survives_a_vector_store_failure():
    handler = _handler()
    handler.graph_store.vec_db = MagicMock()
    handler.graph_store.vec_db.update.side_effect = RuntimeError("qdrant unavailable")

    _merge(handler)

    assert handler.graph_store.add_edge.call_args_list == [
        call("loser", "keeper", type="MERGED_TO", user_name="example-agent")
    ]


def test_merge_without_a_vector_store_still_archives():
    handler = _handler()
    handler.graph_store.vec_db = None

    _merge(handler)

    handler.graph_store.update_node.assert_called_once_with(
        "loser", {"status": "archived"}, user_name="example-agent"
    )


# ──────────────────────────── resolve() 解析 ────────────────────────────


def _conflict_pair():
    def node(item_id, text, created_at):
        return TextualMemoryItem(
            id=item_id,
            memory=text,
            metadata=TreeNodeTextualMemoryMetadata(
                created_at=created_at, embedding=[0.1], memory_type="LongTermMemory"
            ),
        )

    return (
        node(
            str(uuid.uuid5(uuid.NAMESPACE_URL, "newer")), "newer text", "2026-10-16T12:00:00+08:00"
        ),
        node(
            str(uuid.uuid5(uuid.NAMESPACE_URL, "older")), "older text", "2026-10-15T12:00:00+08:00"
        ),
    )


def _resolving_handler(response):
    handler = _handler()
    handler.llm.generate.return_value = response
    handler.graph_store.get_edges.return_value = []
    # 只验证走哪条分支，融合与落库各自另有测试。
    handler._merge_metadata = MagicMock(return_value=TreeNodeTextualMemoryMetadata())
    handler._resolve_in_graph = MagicMock()
    return handler


def _assert_archived_not_deleted(handler, newer, older):
    """融合不出来时只归档较旧的一方，并把它的原文记进新节点的 history；原实现 delete_node，不可逆。"""
    handler._resolve_in_graph.assert_not_called()
    handler.graph_store.delete_node.assert_not_called()
    updates = {c.args[0]: c.args[1] for c in handler.graph_store.update_node.call_args_list}
    assert set(updates) == {newer.id, older.id}
    assert updates[older.id]["status"] == "archived"
    assert "valid_to" in updates[older.id]
    assert [json.loads(h)["memory"] for h in updates[newer.id]["history"]] == ["older text"]
    assert "memory" not in updates[newer.id]
    handler.graph_store.add_edge.assert_called_once_with(
        older.id, newer.id, type="MERGED_TO", user_name="example-agent"
    )


def test_resolve_falls_back_when_the_answer_is_absurdly_long():
    """实测有一次 LLM 返回约 173 MB 的反斜杠，原实现把它原样写进 merged_memory——
    一条日志记录 173 MB（日志文件涨到 176 MB），并准备把这个体积当记忆写进图库。"""
    handler = _resolving_handler("<answer>" + "\\" * 200_000 + "</answer>")
    a, b = _conflict_pair()

    handler.resolve(a, b, "redundant", user_name="example-agent")

    _assert_archived_not_deleted(handler, a, b)


def test_resolve_falls_back_when_there_is_no_answer_tag():
    """re.search 返回 None 时原实现直接 AttributeError，而 except 只接
    JSONDecodeError，等于没接住。"""
    handler = _resolving_handler("I could not decide.")
    a, b = _conflict_pair()

    handler.resolve(a, b, "redundant", user_name="example-agent")

    _assert_archived_not_deleted(handler, a, b)


def test_resolve_falls_back_when_the_answer_is_empty():
    """空 answer 原本会走进 else 分支，拿空串当融合结果覆盖 keeper，把正文清掉。"""
    handler = _resolving_handler("<answer>   </answer>")
    a, b = _conflict_pair()

    handler.resolve(a, b, "redundant", user_name="example-agent")

    _assert_archived_not_deleted(handler, a, b)


def test_resolve_merges_a_normal_answer():
    handler = _resolving_handler("<answer>合并后的偏好</answer>")
    a, b = _conflict_pair()

    handler.resolve(a, b, "redundant", user_name="example-agent")

    handler._merge_metadata.assert_called_once_with("合并后的偏好", a.metadata, b.metadata)
    handler._resolve_in_graph.assert_called_once()


def test_resolve_does_not_log_the_whole_merged_memory(caplog):
    """即便走了融合分支，日志也只留预览——否则正文（含用户内容）整条落盘。"""
    handler = _resolving_handler("<answer>" + "x" * 5_000 + "</answer>")
    a, b = _conflict_pair()

    with caplog.at_level("INFO"):
        handler.resolve(a, b, "redundant", user_name="example-agent")

    resolved = [r.getMessage() for r in caplog.records if "Resolved result" in r.getMessage()]
    assert len(resolved) == 1
    # 预览上限 200 字符 + item 的 repr 前后缀，量级在几百字节；未截断时是 5 KB 以上。
    assert len(resolved[0]) < 500
    assert "truncated," in resolved[0]
    assert resolved[0].count("x") < 300
