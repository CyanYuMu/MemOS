"""S2 and part of S8 against the real model, through the HTTP trial endpoint.

Skipped unless ``MEMRADER_API_KEY`` is set.
"""

import os

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler


pytestmark = pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)

CUBE = "cube-trial-live"
CONVERSATION = {
    "messages": [
        {
            "role": "user",
            "content": "我们把项目的部署方式改成了蓝绿发布，回滚只需要切换流量，不再停机。",
        },
        {"role": "assistant", "content": "明白，蓝绿发布下回滚靠切流量。"},
    ]
}


@pytest.fixture(scope="module")
def app():
    components = {name: Mock() for name in ("graph_db", "mem_reader", "embedder", "mem_scheduler")}
    components.update(
        {
            "llm": Mock(),
            "chat_llms": {},
            "playground_chat_llms": {},
            "reranker": Mock(),
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
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "live.db"))
    return TestClient(app)


def test_chat_trial_returns_structured_memories_and_a_default_comparison(client):
    response = client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "input": CONVERSATION,
            "compare_default": True,
        },
    )
    assert response.status_code == 200, response.text
    runs = response.json()["data"]["runs"]
    assert [run["label"] for run in runs] == ["draft", "default"]
    for run in runs:
        assert run["ok"] is True, run.get("error")
        assert run["parse_ok"] is True, run["raw_output"][:400]
        assert run["fallback_triggered"] is False
        assert run["contract"]["ok"] is True, run["contract"]
        assert run["output"]["memory list"]
        assert run["latency_ms"] > 0
        # usage accumulates every model call of this run and reports how many.
        assert run["usage"]["calls"] >= 1
        # No draft was sent, so the run reports the slot it resolved for itself.
        assert [slot["key"] for slot in run["used_slots"]] == ["extract.chat"]
        assert run["used_slots"][0]["source"] in {"project", "default"}


def test_motive_trial_returns_motives(client):
    response = client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": CUBE,
            "key": "dream.motive",
            "lang": "en",
            "input": {
                "memories": [
                    {"id": "m1", "memory": "The user keeps postponing the schema migration."},
                    {"id": "m2", "memory": "The user said the migration is blocked by a review."},
                ]
            },
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()["data"]["runs"][0]
    assert run["ok"] is True, run.get("error")
    assert run["parse_ok"] is True, run["raw_output"][:400]
    assert isinstance(run["output"], list)
