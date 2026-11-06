"""Tests for the project-level extract prompt and model inventory endpoints."""

from unittest.mock import Mock, patch

import pytest

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def mocked_llm() -> Mock:
    return Mock()


@pytest.fixture(scope="module")
def mock_init_server(mocked_llm):
    mock_components = {
        "graph_db": Mock(),
        "mem_reader": Mock(),
        "llm": mocked_llm,
        "chat_llms": {},
        "playground_chat_llms": {},
        "embedder": Mock(),
        "reranker": Mock(),
        "internet_retriever": Mock(),
        "memory_manager": Mock(),
        "default_cube_config": Mock(),
        "mem_scheduler": Mock(),
        "feedback_server": Mock(),
        "naive_mem_cube": Mock(),
        "searcher": Mock(),
        "api_module": Mock(),
        "text_mem": Mock(),
        "redis_client": None,
        "deepsearch_agent": Mock(),
        "online_bot": None,
    }

    with patch("memos.api.handlers.init_server", return_value=mock_components):
        from fastapi import FastAPI

        from memos.api.routers import server_router

        app = FastAPI()
        app.include_router(server_router.router)
        # The router module is imported once per process. When another test file
        # imported it first, its handlers hold that file's mocks, so point the
        # prompt handler's dependencies at this fixture's LLM explicitly.
        server_router.prompt_handler.deps.llm = mocked_llm
        yield app


@pytest.fixture
def client(mock_init_server, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "prompts.db"))
    return TestClient(mock_init_server)


CUBE = "cube-prompt-test"


def test_default_prompt(client):
    response = client.post("/product/get_extract_prompt", json={"mem_cube_id": CUBE})
    assert response.status_code == 200
    data = response.json()["data"]
    assert "${conversation}" in data["prompt"]
    assert data["is_default"] is True
    assert data["source"] == "default"
    assert data["extraction_types"] == ["text_mem", "pref_mem", "event_mem"]


def test_save_and_read_back(client):
    prompt = "抽取规则：只保留与安全相关的事实。\n\n对话：${conversation}"
    saved = client.post(
        "/product/save_extract_prompt", json={"mem_cube_id": CUBE, "prompt": prompt}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["prompt"] == prompt

    loaded = client.post("/product/get_extract_prompt", json={"mem_cube_id": CUBE})
    data = loaded.json()["data"]
    assert data["prompt"] == prompt
    assert data["is_default"] is False
    assert data["source"] == "saved"
    assert data["updated_at"]


def test_save_rejects_missing_placeholder(client):
    response = client.post(
        "/product/save_extract_prompt", json={"mem_cube_id": CUBE, "prompt": "没有占位符"}
    )
    assert response.status_code == 400
    assert "${conversation}" in response.json()["detail"]


def test_reset_returns_default(client):
    client.post(
        "/product/save_extract_prompt",
        json={"mem_cube_id": CUBE, "prompt": "自定义 ${conversation}"},
    )
    reset = client.post("/product/reset_extract_prompt", json={"mem_cube_id": CUBE})
    assert reset.status_code == 200
    data = reset.json()["data"]
    assert data["prompt"] != "自定义 ${conversation}"
    assert "${conversation}" in data["prompt"]

    loaded = client.post("/product/get_extract_prompt", json={"mem_cube_id": CUBE}).json()["data"]
    assert loaded["is_default"] is True


def test_projects_are_isolated(client):
    client.post(
        "/product/save_extract_prompt",
        json={"mem_cube_id": "cube-a", "prompt": "A ${conversation}"},
    )
    b = client.post("/product/get_extract_prompt", json={"mem_cube_id": "cube-b"}).json()["data"]
    assert b["is_default"] is True


def test_generate_and_debug_use_llm(client, mocked_llm):
    client.post(
        "/product/save_extract_prompt",
        json={"mem_cube_id": CUBE, "prompt": "规则 ${conversation}"},
    )
    mocked_llm.generate.return_value = "重写后的规则 ${conversation}"
    generated = client.post(
        "/product/generate_extract_prompt",
        json={"mem_cube_id": CUBE, "description": "只抽安全事件"},
    )
    assert generated.status_code == 200
    assert generated.json()["data"]["prompt"] == "重写后的规则 ${conversation}"

    mocked_llm.generate.return_value = (
        '{"memory list": [{"memory_type": "LongTermMemory", "value": "用户偏好深色模式"}]}'
    )
    debugged = client.post(
        "/product/debug_extract",
        json={"mem_cube_id": CUBE, "messages": [{"role": "user", "content": "我喜欢深色模式"}]},
    )
    assert debugged.status_code == 200
    results = debugged.json()["data"]["results"]
    assert results == [{"type": "LongTermMemory", "content": "用户偏好深色模式"}]


def test_models_list(client, monkeypatch):
    monkeypatch.setenv("MEMRADER_MODEL", "reader-model")
    monkeypatch.setenv("MOS_CHAT_MODEL", "chat-model")
    monkeypatch.setenv("MOS_EMBEDDER_MODEL", "embed-model")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    monkeypatch.delenv("MOS_RERANKER_MODEL", raising=False)

    response = client.get("/product/models")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 3
    roles = {record["role"] for record in data["records"]}
    assert roles == {"记忆读取", "记忆写入 / 对话", "向量化"}
    embed = next(r for r in data["records"] if r["role"] == "向量化")
    assert "1024 维" in embed["spec"]


def test_saved_prompt_replaces_the_reader_template(tmp_path, monkeypatch):
    """The reader must build its prompt from the project prompt, not PROMPT_DICT."""
    from types import SimpleNamespace

    from memos.mem_reader import simple_struct
    from memos.mem_reader.prompt_config import delete_prompt, save_prompt
    from memos.mem_reader.prompt_runtime import prompt_scope

    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "reader.db"))
    save_prompt("cube-reader", "项目模板\n${conversation}")

    seen: list[str] = []

    def fake_generate(messages):
        seen.append(messages[0]["content"])
        return '{"memory list": [], "summary": ""}'

    reader = object.__new__(simple_struct.SimpleStructMemReader)
    reader.config = SimpleNamespace(remove_prompt_example=False)
    reader._safe_generate = fake_generate

    reader._get_llm_response("用户提到昨天的会议", None)
    assert seen[-1].startswith(simple_struct.PROMPT_DICT["chat"]["zh"][:20])

    with prompt_scope("cube-reader"):
        reader._get_llm_response("用户提到昨天的会议", None)
    assert seen[-1] == "项目模板\n用户提到昨天的会议"

    delete_prompt("cube-reader")
    with prompt_scope("cube-reader"):
        reader._get_llm_response("用户提到昨天的会议", None)
    assert seen[-1].startswith(simple_struct.PROMPT_DICT["chat"]["zh"][:20])
