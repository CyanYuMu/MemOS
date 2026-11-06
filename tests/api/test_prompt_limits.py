"""B4: size limits on generate inputs and on request bodies."""

import json

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler


CUBE = "cube-limits"
VALID_BODY = "只抽取部署相关的事实。\n\n对话：${conversation}"


@pytest.fixture(scope="module")
def app():
    names = ("graph_db", "mem_reader", "embedder", "mem_scheduler", "reranker")
    components = {name: Mock() for name in names}
    components.update(
        {
            "llm": Mock(),
            "chat_llms": {},
            "playground_chat_llms": {},
            "internet_retriever": Mock(),
            "memory_manager": Mock(),
            "default_cube_config": Mock(),
            "feedback_server": Mock(),
            "naive_mem_cube": Mock(),
            "searcher": Mock(),
            "api_module": Mock(),
            "text_mem": Mock(),
            "redis_client": None,
            "deepsearch_agent": Mock(),
            "online_bot": None,
        }
    )
    with patch("memos.api.handlers.init_server", return_value=components):
        from memos.api.routers import server_router

        app = FastAPI()
        app.include_router(server_router.router)
        app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
        app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
        yield app


@pytest.fixture
def client(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "limits.db"))
    test_client = TestClient(app)
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.return_value = VALID_BODY
    return test_client


def _generate(client, **payload):
    body = {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "instruction": "只抽部署"}
    body.update(payload)
    return client.post("/product/prompts/generate", json=body)


def test_generate_accepts_instructions_up_to_2000_characters(client):
    assert _generate(client, instruction="x" * 2000).status_code == 200
    too_long = _generate(client, instruction="x" * 2001)
    assert too_long.status_code == 413
    assert too_long.json()["code"] == "input_too_large"


def test_generate_accepts_base_bodies_up_to_20000_characters(client):
    exact = "${conversation}" + "x" * (20000 - len("${conversation}"))
    assert len(exact) == 20000
    assert _generate(client, base_body=exact).status_code == 200

    too_long = _generate(client, base_body=exact + "x")
    assert too_long.status_code == 413
    assert too_long.json()["code"] == "input_too_large"


def test_test_endpoint_rejects_bodies_over_256_kib(client):
    payload = json.dumps(
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "input": {"messages": [{"role": "user", "content": "x" * (257 * 1024)}]},
        }
    ).encode()
    response = client.post(
        "/product/prompts/test", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_other_prompt_endpoints_reject_bodies_over_512_kib(client):
    payload = json.dumps(
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "x" * (513 * 1024),
        }
    ).encode()
    response = client.post(
        "/product/prompts/save", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_normal_sized_requests_still_work(client):
    assert _generate(client).status_code == 200
    small = client.post(
        "/product/prompts/validate",
        json={"key": "extract.chat", "lang": "zh", "body": VALID_BODY},
    )
    assert small.status_code == 200


# ---------------------------------------------------------------------------
# C2: legacy endpoints
# ---------------------------------------------------------------------------


def test_legacy_generate_does_not_leak_the_upstream_error(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.side_effect = RuntimeError(
        "post http://10.0.0.7:8080/v1/chat/completions failed"
    )

    response = client.post(
        "/product/generate_extract_prompt",
        json={"mem_cube_id": CUBE, "description": "只抽部署"},
    )
    assert response.status_code == 502
    text = response.text
    assert "10.0.0.7" not in text
    assert "chat/completions" not in text
    assert "Traceback" not in text


def test_legacy_debug_extract_does_not_leak_the_upstream_error(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.side_effect = RuntimeError("bearer sk-secret-value rejected")

    response = client.post(
        "/product/debug_extract",
        json={"mem_cube_id": CUBE, "messages": [{"role": "user", "content": "你好"}]},
    )
    assert response.status_code == 502
    assert "sk-secret-value" not in response.text


def test_legacy_debug_extract_limits_the_conversation(client):
    too_many = client.post(
        "/product/debug_extract",
        json={
            "mem_cube_id": CUBE,
            "messages": [{"role": "user", "content": "x"} for _ in range(31)],
        },
    )
    assert too_many.status_code == 413
    assert too_many.json()["code"] == "input_too_large"

    too_long = client.post(
        "/product/debug_extract",
        json={
            "mem_cube_id": CUBE,
            "messages": [{"role": "user", "content": "x" * 12001}],
        },
    )
    assert too_long.status_code == 413


def test_legacy_generate_limits_the_instruction(client):
    response = client.post(
        "/product/generate_extract_prompt",
        json={"mem_cube_id": CUBE, "description": "x" * 2001},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_legacy_endpoints_still_work_for_normal_inputs(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.return_value = "只抽部署。${conversation}"

    generated = client.post(
        "/product/generate_extract_prompt",
        json={"mem_cube_id": CUBE, "description": "只抽部署"},
    )
    assert generated.status_code == 200
    assert generated.json()["data"]["prompt"] == "只抽部署。${conversation}"

    handler.deps.llm.generate.return_value = (
        '{"memory list": [{"memory_type": "UserMemory", "value": "喜欢咖啡"}]}'
    )
    debugged = client.post(
        "/product/debug_extract",
        json={"mem_cube_id": CUBE, "messages": [{"role": "user", "content": "我喜欢咖啡"}]},
    )
    assert debugged.status_code == 200
    assert debugged.json()["data"]["results"] == [{"type": "UserMemory", "content": "喜欢咖啡"}]
