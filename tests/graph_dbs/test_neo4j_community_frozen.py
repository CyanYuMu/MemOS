"""Community backend delete path must also honour the frozen guard."""

from memos.graph_dbs.neo4j_community import Neo4jCommunityGraphDB


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class FakeSession:
    def __init__(self, ids):
        self.ids = ids
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.queries.append(query)
        if "RETURN n.id AS id" in query:
            return FakeResult([{"id": node_id} for node_id in self.ids])
        return FakeResult([])


class FakeDriver:
    def __init__(self, session):
        self.fake_session = session

    def session(self, database=None):
        return self.fake_session


class FakeVec:
    def __init__(self):
        self.deleted = []

    def delete(self, ids):
        self.deleted.extend(ids)


def test_community_delete_excludes_frozen_nodes_and_cleans_vectors():
    graph = object.__new__(Neo4jCommunityGraphDB)
    session = FakeSession(["m1", "m2"])
    graph.driver = FakeDriver(session)
    graph.db_name = "neo4j"
    graph.vec_db = FakeVec()

    deleted = graph.delete_node_by_prams(memory_ids=["m1", "m2"], writable_cube_ids=["tenant-1"])

    assert deleted == 2
    assert graph.vec_db.deleted == ["m1", "m2"]
    delete_query = next(q for q in session.queries if "DETACH DELETE" in q)
    assert "n.status IS NULL OR n.status <> 'frozen'" in delete_query
    ids_query = next(q for q in session.queries if "RETURN n.id AS id" in q)
    assert "n.status IS NULL OR n.status <> 'frozen'" in ids_query
    assert "n.user_name = $cube_id_0" in delete_query
