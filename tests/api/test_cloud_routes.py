"""Routes for the T2/T3 cloud-parity endpoints (contract-level smoke tests)."""

from unittest.mock import Mock, patch

import pytest

from fastapi.testclient import TestClient

from memos.api.cloud_store import JsonMetadataStore
from memos.api.knowledge_base import KnowledgeBaseService, NoopIngestor
from memos.api.profile import ProfileService


@pytest.fixture(scope="module")
def app():
    mock_components = {
        "graph_db": Mock(),
        "mem_reader": Mock(),
        "llm": Mock(),
        "embedder": Mock(),
        "reranker": Mock(),
        "internet_retriever": Mock(),
        "memory_manager": Mock(),
        "default_cube_config": Mock(),
        "mos_server": Mock(),
        "mem_scheduler": Mock(),
        "feedback_server": Mock(),
        "naive_mem_cube": Mock(),
        "searcher": Mock(),
        "api_module": Mock(),
        "vector_db": None,
        "pref_extractor": None,
        "pref_adder": None,
        "pref_retriever": None,
        "pref_mem": None,
        "online_bot": None,
        "chat_llms": Mock(),
        "redis_client": Mock(),
        "deepsearch_agent": Mock(),
    }
    with patch("memos.api.handlers.init_server", return_value=mock_components):
        from memos.api import server_api

        yield server_api.app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def kb_service(tmp_path):
    return KnowledgeBaseService(
        store=JsonMetadataStore(tmp_path / "store"),
        storage_dir=tmp_path / "kb",
        ingestor=NoopIngestor(),
    )


@pytest.fixture
def profile_service(tmp_path):
    return ProfileService(JsonMetadataStore(tmp_path / "store"))


def _post(client, path, payload):
    return client.post(f"/product/{path}", json=payload)


def test_knowledgebase_lifecycle(client, kb_service):
    with patch("memos.api.routers.server_router.get_kb_service", return_value=kb_service):
        created = _post(
            client,
            "create_knowledgebase",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_name": "docs"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["code"] == 0
        kb_id = body["data"]["knowledgebase_id"]

        listed = _post(client, "get_knowledgebase", {"user_id": "u1", "mem_cube_id": "tenant-1"})
        assert listed.json()["data"]["knowledgebase_list"][0]["knowledgebase_id"] == kb_id

        updated = _post(
            client,
            "update_knowledgebase",
            {
                "user_id": "u1",
                "mem_cube_id": "tenant-1",
                "knowledgebase_id": kb_id,
                "knowledgebase_name": "handbook",
            },
        )
        assert updated.json()["data"]["knowledgebase_name"] == "handbook"

        missing = _post(
            client,
            "update_knowledgebase",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_id": "kb_missing"},
        )
        assert missing.json()["code"] == 404

        added = _post(
            client,
            "add_knowledgebase_file",
            {
                "user_id": "u1",
                "mem_cube_id": "tenant-1",
                "knowledgebase_id": kb_id,
                "file": [{"name": "a.txt", "content": "hello"}],
            },
        )
        assert added.status_code == 200
        file_id = added.json()["data"][0]["id"]

        files = _post(
            client,
            "get_knowledgebase_file",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_id": kb_id},
        )
        assert files.json()["data"]["total"] == 1

        downloaded = _post(
            client,
            "download_knowledgebase_file",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "file_id": file_id},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"hello"
        assert "a.txt" in downloaded.headers["content-disposition"]

        deleted = _post(
            client,
            "delete_knowledgebase_file",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "file_ids": [file_id]},
        )
        assert deleted.json()["data"] == {"success": True, "deleted": 1}

        removed = _post(
            client,
            "delete_knowledgebase",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_id": kb_id},
        )
        assert removed.json()["data"]["success"] is True


def test_knowledgebase_upload_accepts_multipart(client, kb_service):
    with patch("memos.api.routers.server_router.get_kb_service", return_value=kb_service):
        kb_id = _post(
            client,
            "create_knowledgebase",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_name": "docs"},
        ).json()["data"]["knowledgebase_id"]

        response = client.post(
            "/product/add_knowledgebase_file",
            data={
                "user_id": "u1",
                "mem_cube_id": "tenant-1",
                "knowledgebase_id": kb_id,
            },
            files={"file": ("upload.md", b"# hi", "text/markdown")},
        )

        assert response.status_code == 200
        assert response.json()["data"][0]["name"] == "upload.md"


def test_knowledgebase_is_tenant_scoped_over_http(client, kb_service):
    with patch("memos.api.routers.server_router.get_kb_service", return_value=kb_service):
        _post(
            client,
            "create_knowledgebase",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "knowledgebase_name": "docs"},
        )

        other = _post(client, "get_knowledgebase", {"user_id": "u2", "mem_cube_id": "tenant-2"})

        assert other.json()["data"]["knowledgebase_list"] == []


def test_profile_lifecycle(client, profile_service):
    template = {
        "basic": {
            "name": {"value": None, "algorithm_updatable": True},
            "city": {"value": None, "algorithm_updatable": False},
        }
    }
    with patch("memos.api.routers.server_router.get_profile_service", return_value=profile_service):
        created = _post(
            client,
            "add_profile_template",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "name": "me", "metadata": template},
        )
        assert created.status_code == 200
        template_id = created.json()["data"]["profile_template_id"]

        bound = _post(
            client,
            "bind_profile_template",
            {
                "user_id": "u1",
                "mem_cube_id": "tenant-1",
                "bind_list": [{"user_id": "u1", "profile_template_id": template_id}],
            },
        )
        assert bound.json()["data"]["profile_list"][0]["status"] == "bound"

        edited = _post(
            client,
            "edit_profile",
            {
                "user_id": "u1",
                "mem_cube_id": "tenant-1",
                "profile_template_id": template_id,
                "metadata": {"basic.name": "Alice"},
            },
        )
        assert edited.json()["data"]["metadata"]["basic.name"]["value"] == "Alice"

        queried = _post(
            client,
            "get_profile",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "profile_template_id": template_id},
        )
        assert (
            queried.json()["data"]["profile_list"][0]["metadata"]["basic.name"]["value"] == "Alice"
        )

        deleted = _post(
            client,
            "delete_profile",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "profile_template_id": template_id},
        )
        assert deleted.json()["data"]["success"] is True

        detached = _post(
            client,
            "delete_profile_template",
            {"user_id": "u1", "mem_cube_id": "tenant-1", "profile_template_id": template_id},
        )
        assert detached.json()["code"] == 0


def test_profile_template_responses_carry_the_documented_fields(client, profile_service):
    ids = {"user_id": "u1", "mem_cube_id": "tenant-1"}
    tree = {"basic": {"name": {"value": "", "algorithm_updatable": True}}}
    with patch("memos.api.routers.server_router.get_profile_service", return_value=profile_service):
        tpl = _post(client, "add_profile_template", {**ids, "name": "me", "metadata": tree})
        template_id = tpl.json()["data"]["profile_template_id"]
        bind_list = [{"user_id": "u1", "profile_template_id": template_id}]
        bound = _post(client, "bind_profile_template", {**ids, "bind_list": bind_list})
        edited = _post(
            client,
            "edit_profile",
            {**ids, "profile_template_id": template_id, "metadata": {"basic.name": "A"}},
        )
        listed = _post(client, "list_profile_template", {**ids, "page": 1, "size": 10}).json()
        got = _post(client, "get_profile_template", {**ids, "profile_template_id": template_id})
        updated = _post(
            client,
            "update_profile_template",
            {**ids, "profile_template_id": template_id, "name": "me2", "metadata": tree},
        )
        removed = _post(
            client, "delete_profile_template", {**ids, "profile_template_id": template_id}
        )

    assert bound.json()["data"]["success"] is True and edited.json()["data"]["success"] is True
    page = listed["data"]
    assert (page["total"], page["current"], page["size"], page["pages"]) == (1, 1, 10, 1)
    assert page["profile_templates"][0]["instance_count"] == 1
    detail = got.json()["data"]["profile_template"]
    assert detail["profile_template_id"] == template_id and detail["create_time"]
    assert updated.json()["data"]["success"] is True
    assert updated.json()["data"]["affected_instances"] == 1
    assert removed.json()["data"]["deleted_instances"] == 1


def test_template_list_with_only_a_size_returns_one_page(client, profile_service):
    ids = {"user_id": "u1", "mem_cube_id": "tenant-1"}
    tree = {"basic": {"name": {"value": "", "algorithm_updatable": True}}}
    with patch("memos.api.routers.server_router.get_profile_service", return_value=profile_service):
        for index in range(12):
            _post(client, "add_profile_template", {**ids, "name": f"t{index}", "metadata": tree})
        page = _post(client, "list_profile_template", {**ids, "size": 10}).json()["data"]

    assert len(page["profile_templates"]) == 10
    assert (page["total"], page["current"], page["size"], page["pages"]) == (12, 1, 10, 2)


def test_new_routes_are_registered(app):
    paths = {route.path for route in app.routes}

    for path in (
        "/product/update_memory",
        "/product/add_status",
        "/product/create_knowledgebase",
        "/product/add_profile_template",
        "/product/get_profile",
    ):
        assert path in paths
