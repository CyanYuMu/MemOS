"""Owner scope must reach the structured graph and BM25 retrieval paths."""

from memos.memories.textual.tree_text_memory.retrieve.recall import GraphMemoryRetriever
from memos.memories.textual.tree_text_memory.retrieve.retrieval_mid_structs import ParsedTaskGoal


class FakeGraph:
    def __init__(self):
        self.metadata_calls = []

    def get_by_metadata(self, filters, user_name=None, status=None, filter=None):
        self.metadata_calls.append({"filters": filters, "filter": filter})
        return []

    def get_nodes(self, ids, include_embedding=False, user_name=None):
        return []


class FakeBM25:
    def search(self, *args, **kwargs):
        return []


OWNER_SCOPE = {"or": [{"owner_type": "user", "owner_id": "u1"}]}


def test_graph_recall_applies_the_owner_filter():
    graph = FakeGraph()
    retriever = GraphMemoryRetriever(graph, embedder=None, bm25_retriever=FakeBM25())

    retriever._graph_recall(
        ParsedTaskGoal(keys=["k1"], tags=["t1", "t2"]),
        "LongTermMemory",
        user_name="tenant-1",
        search_filter=OWNER_SCOPE,
    )

    assert graph.metadata_calls
    assert all(call["filter"] == OWNER_SCOPE for call in graph.metadata_calls)


def test_bm25_recall_applies_the_owner_filter():
    graph = FakeGraph()
    retriever = GraphMemoryRetriever(graph, embedder=None, bm25_retriever=FakeBM25())

    retriever._bm25_recall(
        "query",
        ParsedTaskGoal(keys=["k1"]),
        "LongTermMemory",
        user_name="tenant-1",
        id_filter={"user_id": "u1"},
        search_filter=OWNER_SCOPE,
    )

    assert graph.metadata_calls[0]["filter"] == OWNER_SCOPE
    assert {"field": "user_id", "op": "=", "value": "u1"} in graph.metadata_calls[0]["filters"]
