"""Tests for cloud filter field/source translation."""

import pytest

from memos.api.product_models import APIChatCompleteRequest, APISearchRequest, ChatRequest
from memos.search.filter_mapping import normalize_filter


def test_time_aliases_are_mapped():
    result = normalize_filter(
        {
            "and": [
                {"create_time": {"gte": "2025-01-01"}},
                {"update_time": {"lte": "2025-12-31"}},
            ]
        }
    )

    assert result == {
        "and": [
            {"created_at": {"gte": "2025-01-01"}},
            {"updated_at": {"lte": "2025-12-31"}},
        ]
    }


def test_related_id_expands_to_user_or_agent():
    result = normalize_filter({"and": [{"related_id": ["u1", "u2"]}]})

    assert result == {
        "and": [
            {
                "or": [
                    {"user_id": {"in": ["u1", "u2"]}},
                    {"agent_id": {"in": ["u1", "u2"]}},
                ]
            }
        ]
    }


def test_per_source_roots_are_routed_by_owner_type():
    single = normalize_filter({"user": {"and": [{"create_time": {"gte": "2025-06-01"}}]}})
    both = normalize_filter(
        {
            "user": {"and": [{"tags": {"contains": "mine"}}]},
            "agent": {},
            "public": {"and": [{"tags": {"contains": "shared"}}]},
        }
    )

    assert single == {"and": [{"owner_type": "user"}, {"created_at": {"gte": "2025-06-01"}}]}
    assert both == {
        "or": [
            {"and": [{"owner_type": "user"}, {"tags": {"contains": "mine"}}]},
            {"owner_type": "agent"},
            {"and": [{"owner_type": "public"}, {"tags": {"contains": "shared"}}]},
        ]
    }


def test_per_source_knowledgebase_is_rejected():
    with pytest.raises(ValueError, match="not supported"):
        normalize_filter({"knowledgebase": {"and": [{"tags": {"contains": "x"}}]}})


def test_per_cube_filters_are_normalized():
    result = normalize_filter({"cube_a": {"and": [{"create_time": {"gte": "2025-01-01"}}]}})

    assert result == {"cube_a": {"and": [{"created_at": {"gte": "2025-01-01"}}]}}


def test_bare_condition_root_is_kept_for_backward_compatibility():
    assert normalize_filter({"scene": "chat"}) == {"scene": "chat"}


def test_bare_condition_with_mixed_values_is_kept():
    result = normalize_filter(
        {"scene": "chat", "create_time": {"gte": "2025-01-01"}, "user_id": {"in": ["u1"]}}
    )

    assert result == {
        "and": [
            {"scene": "chat"},
            {"created_at": {"gte": "2025-01-01"}},
            {"user_id": {"in": ["u1"]}},
        ]
    }


def test_bare_related_id_root_expands():
    assert normalize_filter({"related_id": "u1"}) == {
        "or": [{"user_id": {"in": ["u1"]}}, {"agent_id": {"in": ["u1"]}}]
    }


def test_input_is_not_mutated():
    original = {"and": [{"create_time": {"gte": "2025-01-01"}}]}
    snapshot = {"and": [{"create_time": {"gte": "2025-01-01"}}]}

    normalize_filter(original)

    assert original == snapshot


def test_search_request_normalizes_filter():
    req = APISearchRequest(
        query="q",
        user_id="u",
        filter={"and": [{"create_time": {"gte": "2025-01-01"}}]},
    )

    assert req.filter == {"and": [{"created_at": {"gte": "2025-01-01"}}]}


def test_chat_request_normalizes_filter():
    req = ChatRequest(
        user_id="u",
        query="q",
        filter={"and": [{"update_time": {"lte": "2025-01-01"}}]},
    )

    assert req.filter == {"and": [{"updated_at": {"lte": "2025-01-01"}}]}


def test_chat_complete_request_normalizes_filter():
    req = APIChatCompleteRequest(
        user_id="u",
        query="q",
        filter={"and": [{"create_time": {"gte": "2025-01-01"}}]},
    )

    assert req.filter == {"and": [{"created_at": {"gte": "2025-01-01"}}]}
