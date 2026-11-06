"""A1: a Dream draft must actually be the template that runs.

Real model. Each Dream prompt is tried with a draft that appends a detectable
instruction, side by side with the built-in default. The draft's run must show
the marker; the default's run must show neither the draft marker nor a saved
project override.
"""

import json
import os

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler
from memos.mem_reader import prompt_store
from memos.mem_reader.prompt_registry import default_body


pytestmark = pytest.mark.skipif(
    not os.getenv("MEMRADER_API_KEY"), reason="real model test: set MEMRADER_API_KEY"
)

CUBE = "cube-dream-draft"
MEMORIES = {
    "memories": [
        {"id": "m1", "memory": "The user keeps postponing the schema migration."},
        {"id": "m2", "memory": "The user said the migration is blocked by a review."},
    ]
}


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
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "dream-draft.db"))
    return TestClient(app)


CASES = [
    (
        "dream.motive",
        "Every motive description must end with the exact text {marker}.",
    ),
    (
        "dream.reasoning",
        'The "dream_content" field must end with the exact text {marker}.',
    ),
    (
        "dream.context_binding",
        'Every context "key" must be exactly {marker}.',
    ),
    (
        "dream.context_summary",
        'The "key" field must be exactly {marker}.',
    ),
]


@pytest.mark.parametrize(("key", "instruction"), CASES)
def test_dream_trial_runs_the_draft_and_the_default_separately(client, key, instruction):
    draft_marker = "DRAFTMARK-4B7E"
    draft = default_body(key, "en") + "\n\n" + instruction.format(marker=draft_marker)

    response = client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": CUBE,
            "key": key,
            "lang": "en",
            "body": draft,
            "input": MEMORIES,
            "compare_default": True,
        },
    )
    assert response.status_code == 200, response.text
    runs = {run["label"]: run for run in response.json()["data"]["runs"]}
    assert set(runs) == {"draft", "default"}

    assert runs["draft"]["ok"] is True, runs["draft"].get("error")
    assert draft_marker in json.dumps(runs["draft"]["output"], ensure_ascii=False), runs["draft"][
        "raw_output"
    ][:800]

    assert runs["default"]["ok"] is True, runs["default"].get("error")
    assert draft_marker not in json.dumps(runs["default"]["output"], ensure_ascii=False)


def test_default_run_ignores_a_saved_project_override(client):
    """The default side must be the built-in text, not the project's override."""
    project_marker = "PROJECTMARK-11C2"
    saved = client.post(
        "/product/prompts/save",
        json={
            "mem_cube_id": CUBE,
            "key": "dream.motive",
            "lang": "en",
            "body": default_body("dream.motive", "en")
            + f"\n\nEvery motive description must end with the exact text {project_marker}.",
            "base_version_id": 0,
            "author": "reviewer",
        },
    )
    assert saved.status_code == 200, saved.text

    draft_marker = "DRAFTMARK-5D9A"
    prompt_store_reset = client.post(
        "/product/prompts/test",
        json={
            "mem_cube_id": CUBE,
            "key": "dream.motive",
            "lang": "en",
            "body": default_body("dream.motive", "en")
            + f"\n\nEvery motive description must end with the exact text {draft_marker}.",
            "input": MEMORIES,
            "compare_default": True,
        },
    )
    assert prompt_store_reset.status_code == 200, prompt_store_reset.text
    runs = {run["label"]: run for run in prompt_store_reset.json()["data"]["runs"]}

    assert draft_marker in json.dumps(runs["draft"]["output"], ensure_ascii=False)
    assert project_marker not in json.dumps(runs["default"]["output"], ensure_ascii=False)
    assert draft_marker not in json.dumps(runs["default"]["output"], ensure_ascii=False)

    prompt_store.reset_slots(CUBE, key="dream.motive")
