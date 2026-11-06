"""B2: a run must report which template slots it actually used."""

import os

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler


CUBE = "cube-used-slots"


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
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "slots.db"))
    return TestClient(app)


class SlotAwareReader:
    """Stand-in reader: reads the template the way the real one does."""

    def __init__(self) -> None:
        from memos.mem_reader.prompt_registry import default_body
        from memos.mem_reader.prompt_runtime import prompt_for

        self._prompt_for = prompt_for
        self._default = default_body("extract.chat", "zh")
        self.config = type("C", (), {"model_name_or_path": "fake-model"})()
        self.llm = self
        self.general_llm = self
        self.preference_extractor_llm = self
        self.qwen_llm = None
        self.seen: list[str] = []

    def generate(self, messages, **_kwargs):
        self.seen.append(messages[0]["content"])
        return '{"memory list": [], "summary": "s"}'

    def get_memory(self, scenes, **_kwargs):
        from memos.mem_reader.read_multi_modal import detect_lang

        text = str(scenes)
        lang = detect_lang(text) if text else "zh"
        self._prompt_for("extract.chat", lang, self._default)
        return [[]]


@pytest.fixture
def slot_reader(monkeypatch):
    from memos.mem_reader import trial as trial_module

    reader = SlotAwareReader()
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend="simple_struct": reader
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: reader)
    return reader


def _test(client, **payload):
    body = {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    body.update(payload)
    return client.post("/product/prompts/test", json=body)


def test_a_used_draft_is_reported(client, slot_reader):
    response = _test(
        client,
        body="草稿 ${conversation}",
        input={"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]},
    )
    run = response.json()["data"]["runs"][0]
    assert run["used_slots"] == [
        {"key": "extract.chat", "lang": "zh", "source": "draft", "version_id": 0}
    ]
    assert run["warnings"] == []


def test_without_a_draft_the_run_reports_where_the_template_came_from(client, slot_reader):
    response = _test(
        client,
        input={"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]},
    )
    run = response.json()["data"]["runs"][0]
    assert run["used_slots"] == [
        {"key": "extract.chat", "lang": "zh", "source": "default", "version_id": 0}
    ]
    assert run["warnings"] == []


def test_a_draft_for_another_language_is_flagged(client, slot_reader):
    response = _test(
        client,
        body="草稿 ${conversation}",
        input={"messages": [{"role": "user", "content": "We moved CI to self-hosted runners."}]},
    )
    run = response.json()["data"]["runs"][0]
    assert [slot["source"] for slot in run["used_slots"]] == ["default"]
    assert run["used_slots"][0]["lang"] == "en"
    warnings = {warning["code"]: warning["message"] for warning in run["warnings"]}
    assert "draft_not_used" in warnings
    message = warnings["draft_not_used"]
    assert "英文" in message and "中文" in message and "草稿" in message


@pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)
def test_real_model_reports_the_slot_it_actually_used(client):
    english = _test(
        client,
        body="草稿 ${conversation}",
        input={"messages": [{"role": "user", "content": "We moved CI to self-hosted runners."}]},
    ).json()["data"]["runs"][0]
    assert [slot["lang"] for slot in english["used_slots"]] == ["en"]
    assert english["used_slots"][0]["source"] == "default"
    assert any(warning["code"] == "draft_not_used" for warning in english["warnings"])

    chinese = _test(
        client,
        body="草稿 ${conversation}",
        input={"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]},
    ).json()["data"]["runs"][0]
    assert chinese["used_slots"][0] == {
        "key": "extract.chat",
        "lang": "zh",
        "source": "draft",
        "version_id": 0,
    }
    assert chinese["warnings"] == []
