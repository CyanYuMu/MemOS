"""Regression tests for graph-store tenant discovery.

Background maintenance (the structure optimizer) runs without a request
context, so it cannot know which ``user_name`` values actually own data. When
it fell back to ``config.user_name`` while every write carried the cube id,
optimization silently no-opped forever. These tests pin the discovery API that
lets it find the real identities.
"""

from unittest.mock import MagicMock

import pytest

from memos.configs.graph_db import Neo4jGraphDBConfig
from memos.graph_dbs.base import BaseGraphDB
from memos.graph_dbs.neo4j import Neo4jGraphDB


@pytest.fixture
def config():
    return Neo4jGraphDBConfig(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="test",
        db_name="test_memory_db",
        auto_create=False,
        embedding_dimension=3,
        user_name="memosdefault",
        use_multi_db=False,
    )


@pytest.fixture
def graph_db(config):
    """Build the store without opening a real driver connection."""
    graph_db = Neo4jGraphDB.__new__(Neo4jGraphDB)
    graph_db.config = config
    graph_db.db_name = config.db_name
    graph_db.driver = MagicMock()
    return graph_db


def test_neo4j_list_user_names_returns_sorted_distinct(graph_db):
    session = graph_db.driver.session.return_value.__enter__.return_value
    session.run.return_value = [
        {"user_name": "example-agent"},
        {"user_name": "hermes-agent"},
        {"user_name": "example-agent"},
    ]

    assert graph_db.list_user_names() == ["example-agent", "hermes-agent"]

    query = session.run.call_args.args[0]
    assert "DISTINCT n.user_name" in query


def test_neo4j_list_user_names_ignores_empty_values(graph_db):
    session = graph_db.driver.session.return_value.__enter__.return_value
    session.run.return_value = [
        {"user_name": "example-agent"},
        {"user_name": None},
        {"user_name": ""},
    ]

    assert graph_db.list_user_names() == ["example-agent"]


def test_base_implementation_falls_back_to_configured_user_name(graph_db):
    """Backends without logical isolation keep their single-tenant behaviour."""
    assert BaseGraphDB.list_user_names(graph_db) == ["memosdefault"]


def test_base_implementation_returns_empty_without_configured_user_name(graph_db):
    graph_db.config.user_name = None

    assert BaseGraphDB.list_user_names(graph_db) == []
