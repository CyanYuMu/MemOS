"""Tests for the wire contract endpoints: /product/contract and the response shaper."""

from unittest.mock import Mock, patch

import pytest

from fastapi.testclient import TestClient

from memos.api.contract import CONTRACT_VERSION
from memos.api.product_models import GetMemoryGraphResponse, GetMemoryResponse


LEGACY_STAMP = "2026-10-21T10:00:00+00:00"
LEGACY_AS_UTC = "2026-10-21T02:00:00+00:00"


@pytest.fixture(scope="module")
def app():
    """Import server_api with the MemOS stack stubbed out (same trick as test_server_router)."""
    with patch("memos.api.handlers.init_server", return_value=_mock_components()):
        from memos.api import server_api

        yield server_api.app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def memory_handler():
    with patch("memos.api.routers.server_router.handlers.memory_handler") as handler:
        yield handler


def _mock_components():
    mock = Mock()
    return {
        "graph_db": mock,
        "mem_reader": mock,
        "llm": mock,
        "embedder": mock,
        "reranker": mock,
        "internet_retriever": mock,
        "memory_manager": mock,
        "default_cube_config": mock,
        "mos_server": mock,
        "mem_scheduler": mock,
        "feedback_server": mock,
        "naive_mem_cube": mock,
        "searcher": mock,
        "api_module": mock,
        "vector_db": None,
        "pref_extractor": None,
        "pref_adder": None,
        "pref_retriever": None,
        "pref_mem": None,
        "online_bot": None,
        "chat_llms": mock,
        "redis_client": mock,
        "deepsearch_agent": mock,
    }


def _memory_payload(*, time_basis: str | None, sources: list | None = None) -> dict:
    metadata = {"memory_type": "LongTermMemory", "created_at": LEGACY_STAMP}
    if time_basis:
        metadata["time_basis"] = time_basis
    if sources is not None:
        metadata["sources"] = sources
    return {"id": "m1", "memory": "text", "metadata": metadata}


class TestContractEndpoint:
    def test_serves_the_contract_version(self, client):
        response = client.get("/product/contract")
        assert response.status_code == 200
        body = response.json()
        assert body["contract_version"] == CONTRACT_VERSION
        assert body["time"]["basis"] == "utc"
        assert body["sources"]["default"] == "omitted"
        assert body["get_memory_by_ids"]["body"] == "object"
        assert body["top_k_max"] == 25

    def test_health_reports_the_contract_version(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["contract_version"] == CONTRACT_VERSION


class TestResponseShaping:
    def test_legacy_times_are_converted_and_sources_dropped(self, client, memory_handler):
        memory_handler.handle_get_memory.return_value = GetMemoryResponse(
            message="ok", data=_memory_payload(time_basis=None, sources=[{"content": "x"}])
        )

        response = client.get("/product/get_memory/m1")

        assert response.status_code == 200
        metadata = response.json()["data"]["metadata"]
        assert metadata["created_at"] == LEGACY_AS_UTC
        assert "sources" not in metadata

    def test_utc_times_are_stable(self, client, memory_handler):
        memory_handler.handle_get_memory.return_value = GetMemoryResponse(
            message="ok", data=_memory_payload(time_basis="utc")
        )

        response = client.get("/product/get_memory/m1")

        assert response.json()["data"]["metadata"]["created_at"] == LEGACY_STAMP

    def test_include_sources_returns_them_verbatim(self, client, memory_handler):
        memory_handler.handle_get_memory.return_value = GetMemoryResponse(
            message="ok", data=_memory_payload(time_basis="utc", sources=[{"content": "x"}])
        )

        response = client.get("/product/get_memory/m1?include_sources=true")

        assert response.json()["data"]["metadata"]["sources"] == [{"content": "x"}]


class TestGetMemoryByIdsContract:
    def test_object_body_is_the_primary_shape(self, client, memory_handler):
        memory_handler.handle_get_memory_by_ids.return_value = GetMemoryResponse(
            message="ok", data={"memories": [_memory_payload(time_basis="utc")]}
        )

        response = client.post(
            "/product/get_memory_by_ids?mem_cube_id=cube",
            json={"memory_ids": ["m1"], "include_sources": True},
        )

        assert response.status_code == 200
        kwargs = memory_handler.handle_get_memory_by_ids.call_args.kwargs
        assert kwargs["memory_ids"] == ["m1"]
        assert kwargs["mem_cube_id"] == "cube"

    def test_bare_array_is_still_accepted(self, client, memory_handler):
        memory_handler.handle_get_memory_by_ids.return_value = GetMemoryResponse(
            message="ok", data={"memories": [_memory_payload(time_basis="utc")]}
        )

        response = client.post("/product/get_memory_by_ids?mem_cube_id=cube", json=["m1", "m2"])

        assert response.status_code == 200
        kwargs = memory_handler.handle_get_memory_by_ids.call_args.kwargs
        assert kwargs["memory_ids"] == ["m1", "m2"]


class TestGraphProjectionShaping:
    def test_graph_nodes_normalize_camel_case_times(self, client, memory_handler):
        memory_handler.handle_get_memory_graph.return_value = GetMemoryGraphResponse(
            message="ok",
            data={
                "nodes": [
                    {
                        "id": "n1",
                        "memoryType": "LongTermMemory",
                        "time_basis": "",
                        "createdAt": LEGACY_STAMP,
                        "updatedAt": LEGACY_STAMP,
                    }
                ],
                "edges": [],
            },
        )

        response = client.post(
            "/product/get_memory_graph", json={"user_id": "u1", "mem_cube_id": "cube"}
        )

        assert response.status_code == 200
        node = response.json()["data"]["nodes"][0]
        assert node["createdAt"] == LEGACY_AS_UTC
        assert node["updatedAt"] == LEGACY_AS_UTC
