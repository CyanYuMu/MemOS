"""EnhancedBM25 召回在生产图 schema 下的健壮性。

生产图里 activated 节点只有约 40% 带 ``key`` 属性（preference / topic 类节点不写这个
字段），而 BM25 语料构造过去是硬取 ``metadata["key"]``：只要召回集合里有一个节点缺
这个字段，整个 BM25 召回就抛 KeyError 被吞成空列表，等于静默失效。

这里同时锁住三件事：
1. 缺 key 的节点能退化到 topic / memory，不再让整批召回丢失；
2. doc_index 始终映射回传入的 node_dicts 顺序，不能因为过滤/换语料而错位；
3. 同一 corpus_name 下语料发生变化时必须重建索引，否则会把上一轮语料的节点当成
   这一轮的结果返回（脏召回）。
"""

import pytest

from memos.memories.textual.tree_text_memory.retrieve.bm25_util import EnhancedBM25


def _node(node_id, key=None, topic=None, memory="", tags=None):
    metadata = {"tags": tags or []}
    if key is not None:
        metadata["key"] = key
    if topic is not None:
        metadata["topic"] = topic
    return {"id": node_id, "memory": memory, "metadata": metadata}


@pytest.fixture
def bm25():
    return EnhancedBM25()


def test_search_tolerates_nodes_without_key(bm25):
    nodes = [
        _node("1", key="Kubernetes 集群巡检"),
        _node("2", topic="告警 Review 流程"),  # 没有 key
        _node("3", memory="用户偏好固定格式输出"),  # 只有 memory
    ]

    hits = bm25.search("告警", nodes, corpus_name="t-no-key")

    assert hits, "含无 key 节点的语料不应整体丢失"
    assert any(hit["id"] == "2" for hit in hits)


def test_search_returns_nodes_from_given_corpus_only(bm25):
    nodes = [
        _node("1", key="Kubernetes 集群巡检"),
        _node("2", topic="告警 Review 流程"),
        _node("3", key="redis 缓存淘汰策略"),
    ]

    hits = bm25.search("缓存", nodes, corpus_name="t-order")

    assert {hit["id"] for hit in hits} <= {"1", "2", "3"}
    # 命中项必须是传入 node_dicts 里的对象本身，而不是按 doc_index 重新查一遍
    assert any(hit is nodes[2] for hit in hits)


def test_cache_is_invalidated_when_corpus_changes(bm25):
    first_corpus = [_node("a", key="neo4j 图数据库"), _node("b", key="redis 缓存")]
    bm25.search("redis", first_corpus, corpus_name="shared-corpus-name")

    second_corpus = [_node("x", key="qdrant 向量库"), _node("y", key="redis 缓存")]
    hits = bm25.search("redis", second_corpus, corpus_name="shared-corpus-name")

    assert {hit["id"] for hit in hits} <= {"x", "y"}, "语料变了必须重建索引"
    assert "y" in {hit["id"] for hit in hits}


def test_zero_overlap_docs_are_not_returned(bm25):
    """没有任何词重叠的文档不该被塞进候选池。

    BM25 对零重叠文档打 0 分，而 argsort 对全 0 分数会按原始下标返回，等于随机注入
    一批无关记忆，白白消耗 rerank 预算。
    """
    nodes = [_node("1", key="kubernetes 部署"), _node("2", key="redis 缓存")]

    hits = bm25.search("完全无关的查询词", nodes, corpus_name="t-zero-overlap")

    assert hits == []


def test_partial_overlap_keeps_only_matching_docs(bm25):
    nodes = [
        _node("1", key="kubernetes 部署"),
        _node("2", key="redis 缓存淘汰策略"),
        _node("3", key="neo4j 图查询"),
    ]

    hits = bm25.search("缓存", nodes, corpus_name="t-partial-overlap")

    assert {hit["id"] for hit in hits} == {"2"}
