"""E1: the prompt routes must not occupy the event loop.

These go through real HTTP over ``ASGITransport`` and run requests concurrently,
because the defect was not in the handler but in how FastAPI dispatched it: an
``async def`` route calling a blocking handler serialises every request in the
process and the concurrency limits never fire.

The model is a deterministic slow stand-in: this tests the concurrency
bookkeeping, not the model.
"""

import asyncio
import time

from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from memos.api.exceptions import APIExceptionHandler
from memos.mem_reader import trial as trial_module


SLOW_SEC = 1.5
CONVERSATION = {"messages": [{"role": "user", "content": "我们把部署方式改成了蓝绿发布。"}]}
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
def handler():
    return __import__("memos.api.routers.server_router", fromlist=["prompt_handler"]).prompt_handler


@pytest.fixture
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "http-concurrency.db"))


class SlowReader:
    """A reader whose single model call blocks for ``SLOW_SEC``."""

    def __init__(self, started: object) -> None:
        self.started = started
        self.llm = self
        self.general_llm = self
        self.preference_extractor_llm = self
        self.qwen_llm = None
        self.config = SimpleNamespace(model_name_or_path="slow-model")

    def generate(self, *_args, **_kwargs):
        return '{"memory list": [], "summary": "s"}'

    def get_memory(self, *_args, **_kwargs):
        self.started.set()
        time.sleep(SLOW_SEC)
        return [[]]


class SlowLLM:
    """A model client whose rewrite call blocks for ``SLOW_SEC``."""

    def __init__(self, started: object) -> None:
        self.started = started

    def generate(self, *_args, **_kwargs):
        self.started.set()
        time.sleep(SLOW_SEC)
        return VALID_BODY


@pytest.fixture
def slow_trial(monkeypatch):
    """Patch the trial's model plumbing with a slow one; one reader per run."""

    import threading

    started = threading.Event()
    monkeypatch.setattr(
        trial_module, "build_dry_run_reader", lambda _backend=None: SlowReader(started)
    )
    monkeypatch.setattr(trial_module, "build_dry_run_llm", lambda: SlowReader(started))
    return started


def _test_payload(cube: str) -> dict:
    return {"mem_cube_id": cube, "key": "extract.chat", "lang": "zh", "input": CONVERSATION}


async def _post_many(app, path: str, payloads: list[dict]) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.gather(
            *(client.post(path, json=payload) for payload in payloads),
            return_exceptions=False,
        )


def test_concurrent_tests_for_one_project_allow_exactly_one_run(
    app, handler, prompt_db, slow_trial
):
    handler._limiter = trial_module.TrialLimiter(capacity=4)

    responses = asyncio.run(
        _post_many(app, "/product/prompts/test", [_test_payload("busy-cube")] * 3)
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 429, 429], [response.text for response in responses]

    for response in responses:
        if response.status_code == 429:
            body = response.json()
            assert body["code"] == "busy"
            assert body["data"]["retry_after_sec"] > 0


def test_concurrent_tests_across_projects_respect_the_global_capacity(
    app, handler, prompt_db, slow_trial
):
    handler._limiter = trial_module.TrialLimiter(capacity=2)

    responses = asyncio.run(
        _post_many(
            app,
            "/product/prompts/test",
            [_test_payload("cube-a"), _test_payload("cube-b"), _test_payload("cube-c")],
        )
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 200, 429], [response.text for response in responses]


def test_list_responds_while_a_slow_trial_is_running(app, handler, prompt_db, slow_trial):
    handler._limiter = trial_module.TrialLimiter(capacity=4)

    async def scenario() -> tuple[float, float, bool]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            requested_at = time.perf_counter()
            trial = asyncio.create_task(
                client.post("/product/prompts/test", json=_test_payload("slow-cube"))
            )
            # If the loop were blocked, even observing the worker start would
            # wait for the whole run.
            assert await asyncio.to_thread(slow_trial.wait, 5)
            ran_after = time.perf_counter() - requested_at

            listed_at = time.perf_counter()
            listed = await client.post("/product/prompts/list", json={"mem_cube_id": "slow-cube"})
            listed_in = time.perf_counter() - listed_at
            assert listed.status_code == 200
            still_running = not trial.done()
            assert (await trial).status_code == 200
            return ran_after, listed_in, still_running

    ran_after, listed_in, still_running = asyncio.run(scenario())
    assert ran_after < 0.5, (
        f"the loop was blocked for {ran_after:.2f}s before the trial even started"
    )
    assert listed_in < 0.5, f"list waited {listed_in:.2f}s behind the trial"
    assert still_running, "list only ran after the trial had already finished"


def test_list_responds_while_a_slow_generate_is_running(app, handler, prompt_db):
    import threading

    started = threading.Event()
    handler.deps.llm = SlowLLM(started)

    async def scenario() -> tuple[float, float, bool]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            requested_at = time.perf_counter()
            generate = asyncio.create_task(
                client.post(
                    "/product/prompts/generate",
                    json={
                        "mem_cube_id": "slow-cube",
                        "key": "extract.chat",
                        "lang": "zh",
                        "instruction": "只抽部署",
                    },
                )
            )
            assert await asyncio.to_thread(started.wait, 5)
            ran_after = time.perf_counter() - requested_at

            listed_at = time.perf_counter()
            listed = await client.post("/product/prompts/list", json={"mem_cube_id": "slow-cube"})
            listed_in = time.perf_counter() - listed_at
            assert listed.status_code == 200
            still_running = not generate.done()
            assert (await generate).status_code == 200
            return ran_after, listed_in, still_running

    ran_after, listed_in, still_running = asyncio.run(scenario())
    assert ran_after < 0.5, (
        f"the loop was blocked for {ran_after:.2f}s before generate even started"
    )
    assert listed_in < 0.5, f"list waited {listed_in:.2f}s behind generate"
    assert still_running, "list only ran after generate had already finished"
