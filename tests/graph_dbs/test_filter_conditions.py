"""Tests for the recursive Neo4j filter builder."""

from memos.graph_dbs.neo4j import Neo4jGraphDB


def _build(filter_dict):
    return Neo4jGraphDB._build_filter_conditions_cypher(None, filter_dict)


def test_simple_and():
    conditions, params = _build({"and": [{"memory_type": "UserMemory"}, {"status": "activated"}]})

    assert conditions == [
        "(node.memory_type = $filter_memory_type_0) AND (node.status = $filter_status_1)"
    ]
    assert params == {
        "filter_memory_type_0": "UserMemory",
        "filter_status_1": "activated",
    }


def test_nested_or_inside_and():
    conditions, params = _build(
        {
            "and": [
                {"memory_type": "UserMemory"},
                {"or": [{"user_id": "u1"}, {"agent_id": "u1"}]},
            ]
        }
    )

    assert conditions == [
        (
            "(node.memory_type = $filter_memory_type_0) AND "
            "(((node.user_id = $filter_user_id_1) OR (node.agent_id = $filter_agent_id_2)))"
        )
    ]
    assert params["filter_user_id_1"] == "u1"
    assert params["filter_agent_id_2"] == "u1"


def test_time_fields_use_datetime_comparison():
    conditions, _ = _build(
        {
            "and": [
                {"created_at": {"gte": "2025-01-01"}},
                {"chat_time": {"gte": "2025-01-01"}},
            ]
        }
    )

    assert "datetime(node.created_at) >= datetime(" in conditions[0]
    # chat_time is a string property (raw free-form values are preserved), so it
    # must not go through datetime() or a single bad row aborts the query.
    assert "node.chat_time >= $filter_chat_time_gte_" in conditions[0]
    assert "datetime(node.chat_time)" not in conditions[0]


def test_in_operator_inside_or():
    conditions, params = _build(
        {"or": [{"user_id": {"in": ["u1", "u2"]}}, {"agent_id": {"in": ["a1"]}}]}
    )

    assert "node.user_id IN $filter_user_id_in_0" in conditions[0]
    assert "node.agent_id IN $filter_agent_id_in_1" in conditions[0]
    assert params["filter_user_id_in_0"] == ["u1", "u2"]


def test_malformed_logic_raises():
    try:
        _build({"and": "not-a-list"})
    except ValueError as exc:
        assert "array" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ValueError")
