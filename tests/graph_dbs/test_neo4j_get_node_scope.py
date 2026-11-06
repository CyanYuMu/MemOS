"""``get_node`` only filters by ``user_name`` when tenants share a database."""

from unittest.mock import MagicMock, patch

import pytest

from memos.configs.graph_db import Neo4jGraphDBConfig


def _db(*, use_multi_db: bool):
    config = Neo4jGraphDBConfig(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="test",
        db_name="test_db",
        auto_create=False,
        use_multi_db=use_multi_db,
        user_name=None if use_multi_db else "default_user",
        embedding_dimension=3,
    )
    with patch("neo4j.GraphDatabase") as graph_database:
        driver = MagicMock()
        graph_database.driver.return_value = driver
        from memos.graph_dbs.neo4j import Neo4jGraphDB

        db = Neo4jGraphDB(config)
        db.driver = driver
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value.single.return_value = None
    return db, session


@pytest.mark.parametrize(
    ("use_multi_db", "filtered"),
    [(False, True), (True, False)],
    ids=["shared-db", "multi-db"],
)
def test_user_name_filters_the_lookup_only_in_a_shared_database(use_multi_db, filtered):
    db, session = _db(use_multi_db=use_multi_db)

    assert db.get_node("n1", user_name="tenant-a") is None

    query, params = session.run.call_args[0]
    assert ("n.user_name = $user_name" in query) is filtered
    assert params == ({"id": "n1", "user_name": "tenant-a"} if filtered else {"id": "n1"})


def test_a_lookup_without_a_user_name_is_never_filtered():
    db, session = _db(use_multi_db=False)

    db.get_node("n1")

    query, params = session.run.call_args[0]
    assert "user_name" not in query
    assert params == {"id": "n1"}
