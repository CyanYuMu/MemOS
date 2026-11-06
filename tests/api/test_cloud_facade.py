"""Cloud-contract facade: envelope, view split, millisecond times, 4xxxx errors."""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    components = {
        key: Mock()
        for key in (
            "graph_db",
            "mem_reader",
            "llm",
            "embedder",
            "reranker",
            "internet_retriever",
            "memory_manager",
            "default_cube_config",
            "mos_server",
            "mem_scheduler",
            "feedback_server",
            "naive_mem_cube",
            "searcher",
            "api_module",
            "chat_llms",
            "redis_client",
            "deepsearch_agent",
        )
    }
    components.update(dict.fromkeys(("vector_db", "pref_extractor", "pref_adder", "online_bot")))
    with patch("memos.api.handlers.init_server", return_value=components):
        from memos.api import server_api

        yield server_api.app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_get_memory_returns_the_cloud_envelope_and_view_lists(client):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_all.return_value = {
        "nodes": [
            {
                "id": "m1",
                "memory": "likes tea",
                "metadata": {
                    "memory_type": "LongTermMemory",
                    "key": "drink",
                    "created_at": "2026-10-21T10:00:00+00:00",
                    "updated_at": "2026-10-21T10:05:00Z",
                    "session_id": "c1",
                    "status": "activated",
                    "confidence": 0.99,
                    "tags": ["drink"],
                    "sources": [{"type": "chat", "role": "user", "message_id": "x", "lang": "en"}],
                },
            }
        ],
        "total_nodes": 42,
    }

    body = client.post(
        "/get/memory",
        json={"user_id": "u1", "mem_cube_id": "c1", "include_memory_view": ["detail_factual"]},
    ).json()

    item = body["data"]["memory_detail_list"][0]
    assert body["code"] == 0 and body["message"] == "ok"
    assert item["memory_key"] == "drink" and item["memory_value"] == "likes tea"

    def ms(hour, minute):
        return int(datetime(2026, 10, 21, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)

    assert (item["create_time"], item["update_time"]) == (ms(10, 0), ms(10, 5))
    assert item["sources"] == [{"type": "chat", "role": "user", "message_id": "x", "lang": "en"}]
    assert (body["data"]["current"], body["data"]["size"], body["data"]["total"]) == (1, 10, 42)
    assert body["data"]["pages"] == 5


def test_invalid_requests_get_http_400_and_a_cloud_error_code(client):
    missing_user = client.post("/get/memory", json={})
    two_scopes = client.post("/delete/memory", json={"user_id": "u1", "memory_ids": ["m1"]})

    assert missing_user.status_code == 400
    assert missing_user.json()["code"] == 40000
    assert two_scopes.status_code == 400
    assert two_scopes.json()["data"] is None


def test_knowledge_base_and_profile_paths_are_aliased(client, tmp_path):
    from memos.api.cloud_store import JsonMetadataStore
    from memos.api.knowledge_base import KnowledgeBaseService, NoopIngestor
    from memos.api.routers import server_router

    server_router._cloud_store = JsonMetadataStore(tmp_path / "store")
    service = KnowledgeBaseService(
        store=server_router._cloud_store,
        storage_dir=tmp_path / "kb",
        ingestor=NoopIngestor(),
    )
    with patch.object(server_router, "get_kb_service", return_value=service):
        created = client.post(
            "/create/knowledgebase", json={"user_id": "u1", "knowledgebase_name": "docs"}
        ).json()
        listed = client.post("/get/knowledgebase", json={"user_id": "u1"}).json()

    assert created["code"] == 0 and created["data"]["knowledgebase_id"]
    assert listed["code"] == 0


def test_search_budget_is_shared_across_views():
    import copy

    from memos.api.cloud_models import SearchMemoryRequest
    from memos.api.handlers.search_handler import SearchHandler

    results = {
        "text_mem": [
            {
                "cube_id": "c1",
                "memories": [
                    {"id": "fact-a", "metadata": {"relativity": 0.5}},
                    {"id": "fact-b", "metadata": {"relativity": 0.2}},
                ],
            }
        ],
        "pref_mem": [
            {"cube_id": "c1", "memories": [{"id": "pref-a", "metadata": {"relativity": 0.9}}]}
        ],
    }

    trimmed = SearchHandler._apply_view_budget(results, 2)

    assert [item["id"] for item in trimmed["text_mem"][0]["memories"]] == ["fact-a"]
    assert [item["id"] for item in trimmed["pref_mem"][0]["memories"]] == ["pref-a"]

    # The facade turns the budget on privately, and it survives the handler's deepcopy.
    internal = SearchMemoryRequest(query="q", user_id="u1", memory_limit_number=2).to_internal()
    assert copy.deepcopy(internal)._budget_across_views is True


def test_view_budget_keeps_profile_and_tolerates_missing_scores():
    from memos.api.handlers.search_handler import SearchHandler

    profile = {
        "id": "p1",
        "template_id": "t1",
        "relativity": None,
        "metadata": {"memory_type": "ProfileMemory", "relativity": None},
    }
    results = {
        "text_mem": [{"cube_id": "c", "memories": [{"id": "f1", "metadata": {"relativity": 0.5}}]}],
        "profile_mem": [{"cube_id": "c", "memories": [profile]}],
    }

    trimmed = SearchHandler._apply_view_budget(results, 1)

    # Profile fields have no score, so the budget must not rank them against hits.
    assert [item["id"] for item in trimmed["profile_mem"][0]["memories"]] == ["p1"]
    assert [item["id"] for item in trimmed["text_mem"][0]["memories"]] == ["f1"]


def test_view_budget_uses_the_requested_top_k_not_the_dedup_widened_one():
    from types import SimpleNamespace

    from memos.api.handlers.search_handler import SearchHandler
    from memos.api.product_models import APISearchRequest

    def hit(memory_id, score, dim):
        return {
            "id": memory_id,
            "memory": f"text {memory_id}",
            "metadata": {
                "memory_type": "PreferenceMemory"
                if memory_id.startswith("p")
                else "LongTermMemory",
                "status": "activated",
                "relativity": score,
                "embedding": [float(j == dim) for j in range(4)],
                "sources": [],
            },
        }

    class Cube:
        def search_memories(self, req):
            # text_mem is capped at top_k by MMR, pref_mem at pref_top_k, so the
            # candidates before the shared budget are 2 + 4 = 6 > top_k.
            return {
                "text_mem": [{"cube_id": "c", "memories": [hit("f0", 0.9, 0), hit("f1", 0.85, 1)]}],
                "pref_mem": [
                    {
                        "cube_id": "c",
                        "memories": [
                            hit("p0", 0.95, 0),
                            hit("p1", 0.8, 1),
                            hit("p2", 0.75, 2),
                            hit("p3", 0.7, 3),
                        ],
                    }
                ],
            }

    handler = object.__new__(SearchHandler)
    handler.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    handler.deps = SimpleNamespace(reranker=None, graph_db=None, searcher=None, llm=None)
    handler._build_cube_view = lambda req, searcher=None: Cube()
    request = APISearchRequest(user_id="u1", readable_cube_ids=["c"], query="q", top_k=2)
    request._budget_across_views = True

    data = handler.handle_search_memories(request).data
    kept = [
        memory["id"]
        for bucket in ("text_mem", "pref_mem")
        for group in data.get(bucket) or []
        for memory in group.get("memories") or []
    ]

    # Three times top_k (the dedup-widened budget) would keep all six.
    assert sorted(kept) == ["f0", "p0"]


def test_cloud_requests_keep_the_preference_fields_and_omit_the_tool_ones():
    from memos.api.cloud_contract import to_cloud_memory

    preference = to_cloud_memory(
        {
            "id": "m1",
            "memory": "prefers tea",
            "metadata": {
                "memory_type": "PreferenceMemory",
                "preference_type": "like",
                "preference": "prefers tea",
                "reasoning": "said so",
            },
        }
    )
    tool = to_cloud_memory(
        {"id": "m2", "memory": "uses ls", "metadata": {"memory_type": "ToolTrajectoryMemory"}}
    )

    fact = to_cloud_memory(
        {
            "id": "m3",
            "memory": "plain",
            "metadata": {"memory_type": "LongTermMemory", "preference_type": "leak"},
        }
    )

    assert (preference["preference_type"], preference["preference"]) == ("like", "prefers tea")
    assert preference["reasoning"] == "said so"
    assert not {"tool_type", "tool_value"} & set(tool)
    assert "preference_type" not in fact

    empty = to_cloud_memory(
        {
            "id": "m4",
            "memory": "pref",
            "metadata": {"memory_type": "PreferenceMemory", "preference": "", "reasoning": ""},
        }
    )
    assert not {"preference", "reasoning"} & set(empty)


def test_relativity_ignores_non_finite_values():
    from memos.api.handlers.search_handler import SearchHandler

    assert SearchHandler._relativity({"metadata": {"relativity": float("nan")}}) == 0.0
    assert SearchHandler._relativity({"metadata": {"relativity": 10**400}}) == 0.0
    assert SearchHandler._relativity({"relativity": 0.0, "metadata": {"relativity": 0.9}}) == 0.0


def test_chat_forwards_tags_and_the_search_omits_an_unset_relativity():
    from memos.api.cloud_models import ChatCloudRequest, SearchMemoryRequest

    chat = ChatCloudRequest(user_id="u1", query="hi", tags=["a"]).to_internal()
    search = SearchMemoryRequest(query="q", user_id="u1").to_internal()

    assert chat.tags == ["a"]
    # An unset cloud relativity must not be forwarded as an explicit null (that used
    # to fail validation); the model default applies.
    assert search.relativity is not None


def test_handler_http_errors_keep_the_cloud_envelope(client):
    from fastapi import HTTPException

    from memos.api.routers import server_router

    server_router.chat_handler = Mock()
    server_router.chat_handler.handle_chat_complete.side_effect = HTTPException(
        429, detail={"retry": 1}, headers={"Retry-After": "5"}
    )
    rate_limited = client.post("/chat", json={"user_id": "u1", "query": "hi"})

    server_router.chat_handler.handle_chat_complete.side_effect = HTTPException(
        503, detail="Traceback (most recent call last): secret"
    )
    broken = client.post("/chat", json={"user_id": "u1", "query": "hi"})

    assert rate_limited.status_code == 429
    assert rate_limited.json()["code"] == 40000
    assert rate_limited.headers["Retry-After"] == "5"
    assert rate_limited.json()["message"] == '{"retry": 1}'
    assert broken.status_code == 503
    assert broken.json() == {"code": 50000, "data": None, "message": "internal error"}


def test_chat_refuses_streaming_and_wraps_the_answer(client):
    from memos.api.routers import server_router

    server_router.chat_handler = Mock()
    server_router.chat_handler.handle_chat_complete.return_value = {
        "message": "ok",
        "data": {"response": "答", "reasoning": None},
    }

    streamed = client.post("/chat", json={"user_id": "u1", "query": "hi", "stream": True})
    answered = client.post("/chat", json={"user_id": "u1", "query": "hi"}).json()

    assert streamed.status_code == 200 and streamed.json()["code"] == 40000
    assert answered == {"code": 0, "data": {"response": "答"}, "message": "ok"}


def test_app_id_keeps_per_cube_filters(monkeypatch):
    from memos.api.product_models import APISearchRequest

    request = APISearchRequest(
        query="q", user_id="u1", app_id="app1", filter={"cube_a": {"scene": "chat"}}
    )

    assert request.filter == {"cube_a": {"and": [{"scene": "chat"}, {"app_id": "app1"}]}}


def test_agent_only_and_group_requests_need_a_configured_tenant(monkeypatch):
    from pydantic import ValidationError

    from memos.api.cloud_models import (
        AddMessageRequest,
        DeleteMemoryCloudRequest,
        GetMemoryCloudRequest,
    )

    monkeypatch.delenv("MEMOS_CLOUD_TENANT", raising=False)
    for build in (
        lambda: GetMemoryCloudRequest(agent_id="a1"),
        lambda: DeleteMemoryCloudRequest(agent_id="a1").to_internal(),
        lambda: AddMessageRequest(
            user_id=["u1", "u2"], messages=[{"role": "user", "content": "x"}]
        ),
    ):
        with pytest.raises((ValidationError, ValueError), match="MEMOS_CLOUD_TENANT"):
            build()

    monkeypatch.setenv("MEMOS_CLOUD_TENANT", "tenant-x")
    assert DeleteMemoryCloudRequest(agent_id="a1").to_internal().writable_cube_ids == ["tenant-x"]
    group = AddMessageRequest(
        user_id=["u1", "u2"], messages=[{"role": "user", "content": "x"}]
    ).to_internal()
    assert group.writable_cube_ids == ["tenant-x"]
