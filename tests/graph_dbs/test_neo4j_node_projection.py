"""导出图视图时按显式属性投影，避免把数百 MB 的 sources 搬回来。"""

from memos.graph_dbs.neo4j import Neo4jGraphDB


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class FakeSession:
    def __init__(self, keys):
        self.keys = keys
        self.queries = []

    def run(self, query, **params):
        self.queries.append(query)
        return FakeResult([{"k": key} for key in self.keys])


def _graph():
    graph = object.__new__(Neo4jGraphDB)
    return graph


def test_projection_leaves_out_requested_properties():
    session = FakeSession(["id", "memory", "sources", "created_at"])
    projection = _graph()._node_projection(session, ["sources"])

    assert "sources" not in projection
    assert "`id`: n.`id`" in projection
    assert "`created_at`: n.`created_at`" in projection


def test_projection_is_sorted_and_skips_unquotable_keys():
    session = FakeSession(["memory", "id", "weird`key"])

    projection = _graph()._node_projection(session, [])

    assert projection.index("`id`") < projection.index("`memory`")
    assert "weird" not in projection


def test_projection_falls_back_to_whole_node_when_everything_is_excluded():
    session = FakeSession(["sources"])

    assert _graph()._node_projection(session, ["sources"]) == "n"
