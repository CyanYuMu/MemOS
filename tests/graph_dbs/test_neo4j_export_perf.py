"""export_graph 的分页取数、白名单投影与分段计时。"""

from types import SimpleNamespace

from memos.graph_dbs.neo4j import Neo4jGraphDB
from memos.graph_dbs.neo4j_community import Neo4jCommunityGraphDB


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def single(self):
        return self.rows[0]


class FakeSession:
    """按查询形状返回不同结果，并记下所有 Cypher。"""

    def __init__(self, node_rows, edge_rows=None, total_nodes=0, total_edges=0, keys=None):
        self.node_rows = node_rows
        self.edge_rows = edge_rows or []
        self.total_nodes = total_nodes
        self.total_edges = total_edges
        self.keys = keys or ["id", "memory", "sources"]
        self.queries = []

    def run(self, query, params=None, **kwargs):
        self.queries.append(query)
        if "db.propertyKeys" in query:
            return FakeResult([{"k": key} for key in self.keys])
        if "COUNT(n)" in query:
            return FakeResult([{"count": self.total_nodes}])
        if "COUNT(r)" in query:
            return FakeResult([{"count": self.total_edges}])
        if "type(r) AS type" in query:
            return FakeResult(self.edge_rows)
        return FakeResult(self.node_rows)

    def queried(self, needle):
        return [query for query in self.queries if needle in query]


class FakeDriver:
    def __init__(self, session):
        self._session = session

    def session(self, database=None):
        return _SessionContext(self._session)


class _SessionContext:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc_info):
        return False


def _graph(session):
    graph = object.__new__(Neo4jGraphDB)
    graph.driver = FakeDriver(session)
    graph.db_name = "neo4j"
    graph.config = SimpleNamespace(use_multi_db=False, user_name="")
    return graph


def _nodes(count):
    return [{"n": {"id": f"n{i}", "memory": f"m{i}"}} for i in range(count)]


def test_count_is_skipped_when_the_page_is_not_full():
    session = FakeSession(_nodes(2), edge_rows=[], total_nodes=999, total_edges=999)
    graph = _graph(session)

    out = graph.export_graph(user_name="cube-a", page=1, page_size=10)

    assert session.queried("COUNT(n)") == []
    assert session.queried("COUNT(r)") == []
    assert out["total_nodes"] == 2
    assert out["total_edges"] == 0


def test_full_page_trims_the_extra_row_and_runs_the_count():
    session = FakeSession(
        _nodes(11),
        edge_rows=[{"source": "n0", "target": "n1", "type": "PARENT"}] * 11,
        total_nodes=42,
        total_edges=7,
    )
    graph = _graph(session)

    out = graph.export_graph(user_name="cube-a", page=1, page_size=10)

    assert len(out["nodes"]) == 10
    assert len(out["edges"]) == 10
    assert out["total_nodes"] == 42
    assert out["total_edges"] == 7
    assert len(session.queried("COUNT(n)")) == 1
    assert len(session.queried("COUNT(r)")) == 1
    assert any("LIMIT 11" in query for query in session.queried("ORDER BY n.created_at"))


def test_whitelist_projection_skips_property_keys_and_limits_fields():
    session = FakeSession(_nodes(1), keys=["id", "memory", "sources", "embedding"])
    graph = _graph(session)

    graph.export_graph(
        user_name="cube-a",
        page=1,
        page_size=10,
        include_properties=["id", "memory", "created_at"],
    )

    assert session.queried("db.propertyKeys") == []
    node_query = session.queried("ORDER BY n.created_at")[0]
    assert "`id`: n.`id`" in node_query
    assert "`created_at`: n.`created_at`" in node_query
    assert "sources" not in node_query
    assert "embedding" not in node_query


def test_exclude_projection_still_uses_property_keys():
    session = FakeSession(_nodes(1), keys=["id", "memory", "sources"])
    graph = _graph(session)

    graph.export_graph(user_name="cube-a", page=1, page_size=10, exclude_properties=["sources"])

    assert len(session.queried("db.propertyKeys")) == 1
    node_query = session.queried("ORDER BY n.created_at")[0]
    assert "`sources`" not in node_query


def test_timing_collects_each_phase():
    session = FakeSession(_nodes(1))
    graph = _graph(session)
    timing = {}

    graph.export_graph(user_name="cube-a", page=1, page_size=10, timing=timing)

    assert set(timing) == {
        "projection",
        "fetch_nodes",
        "count_nodes",
        "fetch_edges",
        "count_edges",
        "total",
    }
    assert all(value >= 0 for value in timing.values())


def test_timing_is_optional():
    session = FakeSession(_nodes(1))
    graph = _graph(session)

    out = graph.export_graph(user_name="cube-a", page=1, page_size=10)

    assert out["total_nodes"] == 1


class FakeVecDB:
    def __init__(self):
        self.lookups = []

    def get_by_id(self, node_id):
        self.lookups.append(node_id)
        return SimpleNamespace(vector=[0.1, 0.2])


def _community_graph(session):
    graph = object.__new__(Neo4jCommunityGraphDB)
    graph.driver = FakeDriver(session)
    graph.db_name = "neo4j"
    graph.config = SimpleNamespace(use_multi_db=False, user_name="")
    graph.vec_db = FakeVecDB()
    return graph


def test_community_export_skips_one_vector_lookup_per_node():
    session = FakeSession(_nodes(3))
    graph = _community_graph(session)

    out = graph.export_graph(user_name="cube-a", page=1, page_size=10)

    assert graph.vec_db.lookups == []
    assert all("embedding" not in node["metadata"] for node in out["nodes"])


def test_community_export_still_fetches_vectors_when_asked():
    session = FakeSession(_nodes(2))
    graph = _community_graph(session)

    out = graph.export_graph(user_name="cube-a", page=1, page_size=10, include_embedding=True)

    assert graph.vec_db.lookups == ["n0", "n1"]
    assert out["nodes"][0]["metadata"]["embedding"] == [0.1, 0.2]
