"""MMR 需要向量时，先复用向量库里已有的向量，不要每次重新 embed。"""

from types import SimpleNamespace

from memos.api.handlers.search_handler import SearchHandler


class _VecDB:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get_by_ids(self, ids):
        self.calls += 1
        return [i for i in self.items if i.id in ids]


class _Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.5, 0.5] for _ in texts]


def _handler(vec_db, embedder):
    handler = object.__new__(SearchHandler)
    handler.deps = SimpleNamespace(
        naive_mem_cube=SimpleNamespace(
            text_mem=SimpleNamespace(graph_store=SimpleNamespace(vec_db=vec_db))
        ),
        searcher=SimpleNamespace(embedder=embedder),
    )
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None)
    return handler


def test_vectors_are_reused_from_the_store():
    vec_db = _VecDB([SimpleNamespace(id="m1", vector=[0.1, 0.2])])
    embedder = _Embedder()
    handler = _handler(vec_db, embedder)
    memories = [{"id": "m1", "memory": "已经入过库的记忆", "metadata": {}}]

    embeddings = handler._extract_embeddings(memories)

    assert embeddings == [[0.1, 0.2]]
    assert embedder.calls == []  # 没有重新 embed
    assert vec_db.calls == 1


def test_missing_vectors_fall_back_to_embedding():
    vec_db = _VecDB([])
    embedder = _Embedder()
    handler = _handler(vec_db, embedder)
    memories = [{"id": "m2", "memory": "向量库里没有", "metadata": {}}]

    embeddings = handler._extract_embeddings(memories)

    assert embeddings == [[0.5, 0.5]]
    assert embedder.calls == [["向量库里没有"]]


def test_store_errors_fall_back_to_embedding():
    class _Boom:
        def get_by_ids(self, ids):
            raise RuntimeError("qdrant down")

    embedder = _Embedder()
    handler = _handler(_Boom(), embedder)
    memories = [{"id": "m3", "memory": "x", "metadata": {}}]

    assert handler._extract_embeddings(memories) == [[0.5, 0.5]]
    assert embedder.calls == [["x"]]
