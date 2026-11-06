"""``/product/prompts/test`` and ``/product/prompts/generate``.

The trial run is exercised with a stub reader and a stub model so the tests are
deterministic and free; the real-model behaviour is covered by
``tests/mem_reader/test_prompt_live_effect.py`` and ``tests/dream/test_prompt_stages.py``.
"""

import json

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler
from memos.mem_reader import prompt_store
from memos.mem_reader import trial as trial_module


CUBE = "cube-trial"


@pytest.fixture(scope="module")
def mock_init_server():
    components = {
        "graph_db": Mock(),
        "mem_reader": Mock(),
        "llm": Mock(),
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
    with patch("memos.api.handlers.init_server", return_value=components):
        from memos.api.routers import server_router

        app = FastAPI()
        app.include_router(server_router.router)
        app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
        app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
        yield app


@pytest.fixture
def client(mock_init_server, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "trial.db"))
    return TestClient(mock_init_server)


class FakeReader:
    """Mimics the reader surface the trial runner touches."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.llm = self
        self.general_llm = self
        self.preference_extractor_llm = self
        self.qwen_llm = None
        self.config = SimpleNamespace(model_name_or_path="fake-model")

    def generate(self, *_args, **_kwargs):
        return self.reply

    def get_memory(self, scenes, **_kwargs):
        # Go through the (wrapped) model like the real reader does.
        self.llm.generate([{"role": "user", "content": str(scenes)}])
        item = SimpleNamespace(model_dump=lambda mode="json": {"memory": "来自假模型"})
        return [[item]]


@pytest.fixture
def fake_reader(monkeypatch):
    """Route the trial runner at a stub reader and model."""
    reply = (
        '{"memory list": [{"key": "k", "memory_type": "LongTermMemory", '
        '"value": "蓝绿发布", "tags": ["部署"]}], "summary": "s"}'
    )
    state = {"reader": FakeReader(reply)}

    def build(_backend="simple_struct"):
        return state["reader"]

    monkeypatch.setattr(trial_module, "build_dry_run_reader", build)
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: state["reader"])
    return state


def _test(client, **payload):
    body = {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    body.update(payload)
    return client.post("/product/prompts/test", json=body)


def _conversation() -> dict:
    return {"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]}


def test_trial_returns_a_draft_run_with_contract_and_latency(client, fake_reader):
    response = _test(client, input=_conversation())
    assert response.status_code == 200, response.text
    run = response.json()["data"]["runs"][0]
    assert run["label"] == "draft"
    assert run["ok"] is True
    assert run["parse_ok"] is True
    assert run["fallback_triggered"] is False
    assert run["contract"] == {"ok": True, "missing": []}
    assert run["output"]["memory list"]
    assert run["latency_ms"] >= 0
    assert run["model"] == "fake-model"


def test_trial_can_run_the_default_side_by_side(client, fake_reader):
    response = _test(client, input=_conversation(), compare_default=True)
    runs = response.json()["data"]["runs"]
    assert [run["label"] for run in runs] == ["draft", "default"]


def test_trial_reports_fallback_when_the_model_breaks_the_contract(client, fake_reader):
    fake_reader["reader"] = FakeReader("这不是 JSON")
    response = _test(client, input=_conversation())
    run = response.json()["data"]["runs"][0]
    assert run["ok"] is True
    assert run["parse_ok"] is False
    assert run["fallback_triggered"] is True
    assert run["contract"]["ok"] is False


def test_trial_rejects_a_bad_draft_before_calling_the_model(client, fake_reader):
    response = _test(client, body="没有占位符", input=_conversation())
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt"


def test_trial_rejects_a_wrong_input_shape(client, fake_reader):
    response = _test(client, input={"messages": "not-a-list"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"


def test_trial_rejects_an_oversized_conversation(client, fake_reader):
    messages = [{"role": "user", "content": "x" * 500} for _ in range(25)]
    response = _test(client, input={"messages": messages})
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_trial_rejects_an_unknown_key_and_language(client, fake_reader):
    unknown = _test(client, key="extract.nope", input=_conversation())
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "unknown_prompt_key"

    wrong_lang = _test(client, key="dream.motive", lang="zh", input={"memories": []})
    assert wrong_lang.status_code == 400
    assert wrong_lang.json()["code"] == "invalid_input"


def test_trial_returns_busy_when_the_limiter_refuses(client, fake_reader, monkeypatch):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler

    class BusyLimiter:
        def lease(self, _cube_id, runs):
            from memos.exceptions import PromptBusyError

            raise PromptBusyError("busy", 3)

    monkeypatch.setattr(handler, "_limiter", BusyLimiter())
    response = _test(client, input=_conversation())
    assert response.status_code == 429
    assert response.json()["code"] == "busy"
    assert response.json()["data"]["retry_after_sec"] == 3


def test_trial_limiter_allows_one_run_per_project_and_caps_the_total():
    from memos.exceptions import PromptBusyError

    limiter = trial_module.TrialLimiter(capacity=1)
    with limiter.slot("cube-a"):
        with pytest.raises(PromptBusyError), limiter.slot("cube-a"):
            pass
        # The global cap is reached by cube-a's run.
        with pytest.raises(PromptBusyError), limiter.slot("cube-b"):
            pass
    # After release both are free again.
    with limiter.slot("cube-b"):
        pass


def test_trial_lease_releases_after_every_worker_is_done():
    from memos.exceptions import PromptBusyError

    limiter = trial_module.TrialLimiter(capacity=2)
    lease = limiter.lease("cube-a", runs=2)
    lease.done()
    with pytest.raises(PromptBusyError):
        limiter.acquire("cube-a")
    lease.done()
    limiter.acquire("cube-a")
    limiter.release("cube-a")


def test_trial_writes_nothing(client, fake_reader):
    """S12: a trial leaves no prompt version behind."""
    prompt_store.save_slot(CUBE, "extract.chat", "zh", body="草稿外的当前值 ${conversation}")
    before = prompt_store.list_versions(CUBE, "extract.chat", "zh")[0]

    response = _test(
        client, body="草稿 ${conversation}", input=_conversation(), compare_default=True
    )
    assert response.status_code == 200

    after = prompt_store.list_versions(CUBE, "extract.chat", "zh")[0]
    assert after == before
    assert prompt_store.resolve(CUBE, "extract.chat", "zh").body == "草稿外的当前值 ${conversation}"


def test_generate_rewrites_and_validates(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.return_value = "只抽取部署相关的事实。\n\n对话：${conversation}"

    response = client.post(
        "/product/prompts/generate",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "instruction": "只抽取部署相关的事实",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["body"].startswith("只抽取部署相关的事实。")
    assert data["validation"]["ok"] is True


def test_generate_retries_once_when_the_result_is_invalid(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.side_effect = [
        "没有占位符",
        "修好了 ${conversation}",
    ]

    response = client.post(
        "/product/prompts/generate",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "instruction": "改一下",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["validation"]["ok"] is True
    assert handler.deps.llm.generate.call_count == 2


def test_generate_returns_the_validation_when_the_retry_also_fails(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.return_value = "还是没有占位符"

    response = client.post(
        "/product/prompts/generate",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "instruction": "改一下",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["validation"]["ok"] is False
    assert data["validation"]["errors"][0]["code"] == "missing_placeholder"


def test_a_run_that_cannot_start_does_not_leak_the_trial_slot(
    client, mock_init_server, monkeypatch
):
    from fastapi.testclient import TestClient

    from memos.mem_reader import trial as trial_module

    attempts = {"n": 0}
    reader = FakeReader('{"memory list": [], "summary": "s"}')

    def flaky_llm():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("model client could not start")
        return reader

    monkeypatch.setattr(trial_module, "build_dry_run_llm", flaky_llm)
    monkeypatch.setattr(trial_module, "build_dry_run_reader", lambda _backend=None: reader)

    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler._limiter = trial_module.TrialLimiter(capacity=1)

    raw_client = TestClient(mock_init_server, raise_server_exceptions=False)
    failed = raw_client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "input": _conversation(),
            "compare_default": True,
        },
    )
    assert failed.status_code >= 500

    # The default run never started; its share of the lease must be given back.
    again = raw_client.post(
        "/product/prompts/test",
        json={"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "input": _conversation()},
    )
    assert again.status_code == 200, again.text

    other = raw_client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": "another-cube",
            "key": "extract.chat",
            "lang": "zh",
            "input": _conversation(),
        },
    )
    assert other.status_code == 200, other.text


def test_generate_maps_model_failures_to_502(client):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler
    handler.deps.llm = Mock()
    handler.deps.llm.generate.side_effect = RuntimeError("upstream down")

    response = client.post(
        "/product/prompts/generate",
        json={
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "instruction": "改一下",
        },
    )
    assert response.status_code == 502
    assert response.json()["code"] == "llm_error"
    assert "upstream" not in response.json()["message"]


class _RoleLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.config = SimpleNamespace(model_name_or_path="role-model")

    def generate(self, *_args, **_kwargs):
        return self.reply


class MultiCallReader:
    """One run, several model calls: extraction first, an auxiliary call after."""

    def __init__(self, extraction_reply: str, aux_reply: str = '{"aux": true}') -> None:
        self.llm = _RoleLLM(extraction_reply)
        self.general_llm = _RoleLLM(aux_reply)
        self.preference_extractor_llm = _RoleLLM(aux_reply)
        self.qwen_llm = None
        self.config = SimpleNamespace(model_name_or_path="role-model")

    def get_memory(self, scenes, **_kwargs):
        from memos.mem_reader.prompt_runtime import prompt_for

        prompt_for("extract.chat", "zh", "内置")
        self.llm.generate([{"role": "user", "content": "提取"}])
        self.general_llm.generate([{"role": "user", "content": "辅助"}])
        return [[]]


def test_raw_output_is_the_extraction_call_even_with_other_calls_in_the_run(client, monkeypatch):
    from memos.mem_reader import trial as trial_module

    reader = MultiCallReader('{"memory list": [], "summary": "EXTRACTION-REPLY"}')
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend="simple_struct": reader
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: reader.llm)

    response = _test(client, input=_conversation())
    run = response.json()["data"]["runs"][0]
    assert "EXTRACTION-REPLY" in run["raw_output"]
    assert "aux" not in run["raw_output"]


class CountingReader(MultiCallReader):
    """Reports two model calls the way a real client reports its usage."""

    def get_memory(self, scenes, **_kwargs):
        from memos.llms import usage as llm_usage

        for _ in range(2):
            llm_usage.record(SimpleNamespace(prompt_tokens=10, completion_tokens=5))
        return [[]]


def test_usage_counts_every_model_call_of_a_run(client, monkeypatch):
    from memos.mem_reader import trial as trial_module

    reader = CountingReader("{}")
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend="simple_struct": reader
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: reader.llm)

    run = _test(client, input=_conversation()).json()["data"]["runs"][0]
    assert run["usage"]["calls"] == 2
    assert run["usage"]["total_tokens"] == 30


def test_compare_default_keeps_each_runs_raw_output(client, monkeypatch):
    from memos.mem_reader import trial as trial_module

    class EchoReader(MultiCallReader):
        def __init__(self):
            super().__init__("{}")
            self.raw_llm = self.llm

        def get_memory(self, scenes, **_kwargs):
            from memos.mem_reader.prompt_runtime import prompt_for

            template = prompt_for("extract.chat", "zh", "内置")
            self.raw_llm.reply = json.dumps(
                {"memory list": [], "summary": template[-24:]}, ensure_ascii=False
            )
            self.llm.generate([{"role": "user", "content": "提取"}])
            return [[]]

    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend="simple_struct": EchoReader()
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: EchoReader().raw_llm)

    response = _test(
        client,
        body="草稿 ${conversation}\n\n结尾标记 DRAFT-TAIL-77",
        input=_conversation(),
        compare_default=True,
    )
    runs = {run["label"]: run for run in response.json()["data"]["runs"]}
    assert "DRAFT-TAIL-77" in runs["draft"]["raw_output"]
    assert "DRAFT-TAIL-77" not in runs["default"]["raw_output"]
    assert runs["draft"]["raw_output"] != runs["default"]["raw_output"]
