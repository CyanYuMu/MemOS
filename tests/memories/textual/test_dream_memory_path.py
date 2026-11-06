"""跨案例综合记忆（DreamDiary / InsightMemory）作为附加位接入召回。

这两类此前一条都进不了召回，原因有两个，都要覆盖到：
1. PATH-A~F 的 memory_type 白名单里没有它们；
2. 它们的状态不是 activated（DreamDiary 是 completed / context_only），
   而 _graph_recall 与 _vector_recall 都写死 status="activated"。

所以这里重点断言：走的是 graph_store 直连 + status=None，且正文取 summary。
"""

from unittest.mock import MagicMock

import pytest

from memos.memories.textual.item import (
    SearchedTreeNodeTextualMemoryMetadata,
    TextualMemoryItem,
    TreeNodeTextualMemoryMetadata,
)
from memos.memories.textual.tree_text_memory.retrieve.searcher import Searcher
from memos.reranker.base import BaseReranker


DREAM_ID = "11111111-1111-5111-8111-111111111111"
INSIGHT_ID = "22222222-2222-5222-8222-222222222222"
FACT_ID = "33333333-3333-5333-8333-333333333333"


@pytest.fixture
def searcher():
    s = Searcher(MagicMock(), MagicMock(), MagicMock(), MagicMock(spec=BaseReranker))
    s.task_goal_parser = MagicMock()
    s.graph_retriever = MagicMock()
    s.reasoner = MagicMock()
    return s


def dream_node(mid, summary, memory_type="DreamDiary", title="跨案例摘要"):
    return {
        "id": mid,
        "memory": "正文" * 300,
        "metadata": {
            "memory_type": memory_type,
            "summary": summary,
            "title": title,
            "status": "completed",
        },
    }


def fact(mid, text, memory_type="LongTermMemory"):
    return TextualMemoryItem(
        id=mid, memory=text, metadata=TreeNodeTextualMemoryMetadata(memory_type=memory_type)
    )


def test_queries_both_scopes_with_status_disabled(searcher):
    """两类都查，且必须绕开 status='activated'（它们的状态是 completed）。"""
    searcher.graph_store.search_by_embedding.side_effect = lambda scope, **kw: (
        [{"id": DREAM_ID, "score": 0.8}] if scope == "DreamDiary" else [{"id": INSIGHT_ID, "score": 0.6}]
    )
    nodes = {
        DREAM_ID: dream_node(DREAM_ID, "同一批仿冒站共享中继与统计 ID"),
        INSIGHT_ID: dream_node(INSIGHT_ID, "证据不完整时结论要收敛", memory_type="InsightMemory"),
    }
    searcher.graph_store.get_nodes.side_effect = lambda ids, **kw: [nodes[i] for i in ids if i in nodes]

    items = searcher._retrieve_from_dream_memory([[0.1] * 4], top_k=1, user_name="example-agent")

    scopes = [c.kwargs["scope"] for c in searcher.graph_store.search_by_embedding.call_args_list]
    assert scopes == ["DreamDiary", "InsightMemory"]
    for call in searcher.graph_store.search_by_embedding.call_args_list:
        assert call.kwargs["status"] is None, "不能带 status，否则 completed 的 DreamDiary 全被过滤掉"
        assert call.kwargs["user_name"] == "example-agent"

    assert [i.id for i, _ in items] == [DREAM_ID, INSIGHT_ID], "按分数降序"


def test_returns_summary_not_body(searcher):
    searcher.graph_store.search_by_embedding.return_value = [{"id": DREAM_ID, "score": 0.7}]
    searcher.graph_store.get_nodes.return_value = [dream_node(DREAM_ID, "  多   空格 的摘要  ")]

    items = searcher._retrieve_from_dream_memory([[0.1] * 4], top_k=1)

    assert items[0][0].memory == "多 空格 的摘要", "正文换成 summary，且空白归一"
    assert "正文" not in items[0][0].memory


def test_skips_nodes_without_summary(searcher):
    searcher.graph_store.search_by_embedding.return_value = [{"id": DREAM_ID, "score": 0.7}]
    searcher.graph_store.get_nodes.return_value = [dream_node(DREAM_ID, "")]

    assert searcher._retrieve_from_dream_memory([[0.1] * 4], top_k=1) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 0},  # 配额为 0 → 完全不查
        {"top_k": 1, "memory_type": "LongTermMemory"},  # 类型被限定 → 不越界返回
    ],
)
def test_gated_off(searcher, kwargs):
    assert searcher._retrieve_from_dream_memory([[0.1] * 4], **kwargs) == []
    searcher.graph_store.search_by_embedding.assert_not_called()


def test_no_embedding_is_noop(searcher):
    assert searcher._retrieve_from_dream_memory(None, top_k=1) == []
    searcher.graph_store.search_by_embedding.assert_not_called()


def test_backend_failure_does_not_break_search(searcher):
    """附加位取不到时，整次检索不能失败。"""
    searcher.graph_store.search_by_embedding.side_effect = RuntimeError("neo4j down")

    assert searcher._retrieve_from_dream_memory([[0.1] * 4], top_k=1) == []


def test_sort_and_trim_appends_as_extra_slot(searcher):
    """附加位是纯增量：text_mem 的池子不变，dream 单独占位。"""
    dream = TextualMemoryItem(
        id=DREAM_ID, memory="跨案例摘要", metadata=SearchedTreeNodeTextualMemoryMetadata(
            memory_type="DreamDiary", relativity=0.3
        )
    )
    results = [
        (fact(FACT_ID, "一条长期记忆"), 0.9),
        (dream, 0.3),
    ]

    out = searcher._sort_and_trim(results, top_k=10, include_dream_memory=True, dream_mem_top_k=1)

    types = [i.metadata.memory_type for i in out]
    assert types.count("DreamDiary") == 1
    assert types.count("LongTermMemory") == 1, "原有 text_mem 结果一条不少"


def test_dream_does_not_consume_text_pool(searcher):
    """即使 dream 分数比 text 结果高，也不该挤掉 text 的名额。"""
    dream = TextualMemoryItem(
        id=DREAM_ID, memory="跨案例摘要",
        metadata=SearchedTreeNodeTextualMemoryMetadata(memory_type="DreamDiary", relativity=0.99),
    )
    results = [(dream, 0.99), (fact(FACT_ID, "一条长期记忆"), 0.5)]

    out = searcher._sort_and_trim(results, top_k=1, include_dream_memory=True, dream_mem_top_k=1)

    assert {i.metadata.memory_type for i in out} == {"DreamDiary", "LongTermMemory"}


def test_off_by_default(searcher):
    dream = TextualMemoryItem(
        id=DREAM_ID, memory="跨案例摘要",
        metadata=SearchedTreeNodeTextualMemoryMetadata(memory_type="DreamDiary", relativity=0.9),
    )
    results = [(dream, 0.9), (fact(FACT_ID, "一条长期记忆"), 0.5)]

    out = searcher._sort_and_trim(results, top_k=10)

    assert [i.metadata.memory_type for i in out] == ["LongTermMemory"], "默认不开，行为与改动前一致"


def test_quota_limits_slots(searcher):
    dreams = [
        TextualMemoryItem(
            id=DREAM_ID, memory="摘要一",
            metadata=SearchedTreeNodeTextualMemoryMetadata(memory_type="DreamDiary", relativity=0.9),
        ),
        TextualMemoryItem(
            id=INSIGHT_ID, memory="摘要二",
            metadata=SearchedTreeNodeTextualMemoryMetadata(memory_type="InsightMemory", relativity=0.8),
        ),
    ]
    out = searcher._sort_and_trim([(d, 0.9 - i) for i, d in enumerate(dreams)], top_k=10,
                                  include_dream_memory=True, dream_mem_top_k=1)
    assert len(out) == 1


def test_post_process_puts_dreams_in_own_bucket():
    from memos.api.handlers.formatters_handler import post_process_textual_mem

    result = {
        "text_mem": [], "act_mem": [], "para_mem": [], "pref_mem": [],
        "pref_note": "", "tool_mem": [], "dream_mem": [],
    }
    formatted = [
        {"id": FACT_ID, "memory": "普通记忆", "metadata": {"memory_type": "LongTermMemory"}},
        {"id": DREAM_ID, "memory": "跨案例摘要", "metadata": {"memory_type": "DreamDiary"}},
    ]

    out = post_process_textual_mem(result, formatted, "example-agent")

    assert [m["id"] for m in out["text_mem"][0]["memories"]] == [FACT_ID]
    assert [m["id"] for m in out["dream_mem"][0]["memories"]] == [DREAM_ID]
