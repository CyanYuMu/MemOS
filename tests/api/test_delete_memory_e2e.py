"""T2.2 end-to-end delete: graph nodes and vectors are both removed."""

from types import SimpleNamespace

from memos.api.handlers.memory_handler import handle_delete_memories
from memos.api.product_models import DeleteMemoryRequest
from memos.graph_dbs.neo4j import Neo4jGraphDB


class FakeVecDB:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted.extend(ids)


class FakePrefVecDB:
    def __init__(self):
        self.filters = []

    def delete_by_filter(self, collection_name, filter):
        self.filters.append((collection_name, filter))


class FakeResult:
    def __init__(self, records=None, deleted=0):
        self._records = records or []
        self._deleted = deleted

    def __iter__(self):
        return iter(self._records)

    def consume(self):
        return SimpleNamespace(counters=SimpleNamespace(nodes_deleted=self._deleted))


class FakeSession:
    """Minimal Neo4j session for ``delete_node_by_prams``."""

    def __init__(self, nodes):
        self.nodes = nodes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **params):
        if "RETURN n.id AS id" in query:
            ids = self._matching(params)
            return FakeResult([{"id": node_id} for node_id in ids])

        if "DETACH DELETE" in query:
            ids = self._matching(params)
            for node_id in ids:
                self.nodes.pop(node_id, None)
            return FakeResult(deleted=len(ids))

        raise AssertionError(f"unexpected query: {query}")

    def _matching(self, params):
        ids = list(self.nodes)
        if "memory_ids" in params:
            wanted = set(params["memory_ids"])
            ids = [node_id for node_id in ids if node_id in wanted]
        if params.get("user_name"):
            tenant = params["user_name"]
            ids = [node_id for node_id in ids if self.nodes[node_id]["user_name"] == tenant]
        if "filter_user_name_" in str(params):
            tenant = next(value for key, value in params.items() if key.startswith("filter_user_name_"))
            ids = [node_id for node_id in ids if self.nodes[node_id].get("user_name") == tenant]
        return ids


class FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self, database=None):
        return self._session


def _graph(nodes):
    graph = object.__new__(Neo4jGraphDB)
    graph.config = SimpleNamespace(use_multi_db=False, user_name="tenant-1")
    graph.db_name = "test"
    graph.driver = FakeDriver(FakeSession(nodes))
    graph.vec_db = FakeVecDB()
    return graph


class FakeTextMem:
    def __init__(self, graph, nodes=None):
        self.graph_store = graph
        self.nodes = nodes if nodes is not None else graph.driver._session.nodes
        self.deleted_ids = []

    def delete_by_filter(self, writable_cube_ids=None, file_ids=None, filter=None):
        self.graph_store.delete_node_by_prams(
            writable_cube_ids=writable_cube_ids, file_ids=file_ids, filter=filter
        )

    def delete_by_memory_ids(self, memory_ids):
        self.deleted_ids.extend(memory_ids)
        self.graph_store.delete_node_by_prams(memory_ids=memory_ids)

    def get_all(self, user_name=None, filter=None, **kwargs):
        nodes = list(self.nodes.values())
        for condition in (filter or {}).get("and", []):
            for key, value in condition.items():
                nodes = [node for node in nodes if node.get(key) == value]
        return {"nodes": nodes, "total_nodes": len(nodes)}

    def get_by_ids(self, memory_ids):
        return [self.nodes[mid] for mid in memory_ids if mid in self.nodes]


class FakePrefMem:
    def __init__(self):
        self.vec_db = FakePrefVecDB()

    def delete_by_filter(self, filter):
        for collection in ("explicit_preference", "implicit_preference"):
            self.vec_db.delete_by_filter(collection, filter)


def _nodes():
    return {
        "n1": {"id": "n1", "user_name": "tenant-1", "status": "activated"},
        "n2": {"id": "n2", "user_name": "tenant-1", "status": "activated"},
        "other": {"id": "other", "user_name": "tenant-2", "status": "activated"},
    }


def test_delete_user_removes_graph_nodes_and_vectors():
    nodes = _nodes()
    graph = _graph(nodes)
    cube = SimpleNamespace(text_mem=FakeTextMem(graph), pref_mem=FakePrefMem())

    resp = handle_delete_memories(DeleteMemoryRequest(user_id="tenant-1"), cube)

    assert resp.data["status"] == "success"
    assert set(nodes) == {"other"}  # tenant-2 survived
    assert sorted(graph.vec_db.deleted) == ["n1", "n2"]


def test_delete_user_also_clears_preference_vectors():
    graph = _graph(_nodes())
    cube = SimpleNamespace(text_mem=FakeTextMem(graph), pref_mem=FakePrefMem())

    handle_delete_memories(DeleteMemoryRequest(user_id="tenant-1"), cube)

    assert len(cube.pref_mem.vec_db.filters) == 2
    for _, filter in cube.pref_mem.vec_db.filters:
        assert filter == {"and": [{"user_id": "tenant-1"}]}


def test_explicit_delete_removes_vector_points():
    nodes = _nodes()
    graph = _graph(nodes)
    cube = SimpleNamespace(text_mem=FakeTextMem(graph), pref_mem=None)

    resp = handle_delete_memories(DeleteMemoryRequest(memory_ids=["n1"]), cube)

    assert resp.data["status"] == "success"
    assert "n1" not in nodes
    assert graph.vec_db.deleted == ["n1"]


def test_frozen_memory_is_not_deleted_by_explicit_id():
    nodes = {
        "frozen": {"id": "frozen", "user_name": "tenant-1", "status": "frozen"},
    }
    graph = _graph(nodes)
    text_mem = FakeTextMem(graph, nodes=nodes)
    cube = SimpleNamespace(text_mem=text_mem, pref_mem=None)

    resp = handle_delete_memories(DeleteMemoryRequest(memory_ids=["frozen"]), cube)

    assert resp.data["status"] == "failure"
    assert "frozen" in resp.message
    assert "frozen" in nodes
    assert graph.vec_db.deleted == []


def test_frozen_memory_is_skipped_by_filter_delete():
    nodes = {
        "ok": {"id": "ok", "user_name": "tenant-1", "status": "activated"},
        "frozen": {"id": "frozen", "user_name": "tenant-1", "status": "frozen"},
    }
    graph = _graph(nodes)
    text_mem = FakeTextMem(graph, nodes=nodes)
    cube = SimpleNamespace(text_mem=text_mem, pref_mem=None)

    resp = handle_delete_memories(DeleteMemoryRequest(user_id="tenant-1"), cube)

    assert resp.data["status"] == "success"
    assert text_mem.deleted_ids == ["ok"]
    assert "frozen" in nodes
    assert graph.vec_db.deleted == ["ok"]
