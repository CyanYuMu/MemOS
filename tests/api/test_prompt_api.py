"""The ``/product/prompts/*`` endpoints.

These tests pin the response shapes the control plane proxies and the stable
error codes it maps: ``invalid_prompt`` / ``unknown_prompt_key`` /
``version_conflict``.
"""

import json

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler


CUBE = "cube-prompt-api"


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
        from memos.api.routers.server_router import router

        app = FastAPI()
        app.include_router(router)
        app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
        app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
        yield app


@pytest.fixture
def client(mock_init_server, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "prompts.db"))
    return TestClient(mock_init_server)


def _post(client, name, payload):
    return client.post(f"/product/prompts/{name}", json=payload)


def test_list_returns_every_prompt_without_bodies(client):
    response = _post(client, "list", {"mem_cube_id": CUBE})
    assert response.status_code == 200, response.text
    prompts = response.json()["data"]["prompts"]
    assert len(prompts) == 12
    keys = {item["key"] for item in prompts}
    assert {"extract.chat", "process.hallucination_filter", "dream.motive"} <= keys

    chat = next(item for item in prompts if item["key"] == "extract.chat")
    assert "body" not in chat and "defaults" not in chat
    assert chat["state"]["zh"] == {
        "source": "default",
        "version_id": 0,
        "version_no": 0,
        "updated_at": None,
        "author": None,
    }
    assert chat["languages"] == ["zh", "en"]


def test_detail_returns_registration_defaults_and_effective_bodies(client):
    response = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["group"] == "extract"
    assert data["syntax"] == "dollar"
    assert data["placeholders"][0]["token"] == "${conversation}"
    assert "${conversation}" in data["defaults"]["zh"]
    assert data["effective"]["zh"]["source"] == "default"
    assert data["output_contract"]["required_keys"] == ["memory list", "summary"]

    dream = _post(client, "detail", {"mem_cube_id": CUBE, "key": "dream.motive"}).json()["data"]
    assert dream["locked_suffix"].startswith("\n\nOUTPUT FORMAT (STRICT)")
    assert dream["languages"] == ["en"]


def test_detail_rejects_an_unknown_key_with_a_stable_code(client):
    response = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.nope"})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "unknown_prompt_key"
    assert body["message"]


def test_validate_is_always_200(client):
    ok = _post(
        client,
        "validate",
        {"key": "extract.chat", "lang": "zh", "body": "只抽取部署。\n\n${conversation}"},
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["ok"] is True
    assert data["errors"] == []
    assert [w["code"] for w in data["warnings"]] == ["missing_output_key"]

    bad = _post(client, "validate", {"key": "extract.chat", "lang": "zh", "body": "没有占位符"})
    assert bad.status_code == 200
    bad_data = bad.json()["data"]
    assert bad_data["ok"] is False
    assert bad_data["errors"][0]["code"] == "missing_placeholder"
    assert "占位符" in bad_data["errors"][0]["message"]


def test_save_creates_a_version_and_a_state(client):
    saved = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "甲 ${conversation}",
            "note": "第一次",
            "author": "alice",
            "base_version_id": 0,
        },
    )
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["version_id"] == 1
    assert data["created_at"]
    assert data["effective"] == {
        "body": "甲 ${conversation}",
        "source": "project",
        "version_id": 1,
        "version_no": 1,
        "updated_at": data["created_at"],
        "author": "alice",
    }

    chat = next(
        item
        for item in _post(client, "list", {"mem_cube_id": CUBE}).json()["data"]["prompts"]
        if item["key"] == "extract.chat"
    )
    assert chat["state"]["zh"]["source"] == "project"
    assert chat["state"]["zh"]["version_id"] == 1
    assert chat["state"]["en"]["source"] == "default"

    versions = _post(
        client, "versions", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    ).json()["data"]
    assert [v["version_id"] for v in versions["versions"]] == [1]
    assert versions["versions"][0]["note"] == "第一次"
    assert versions["has_more"] is False


def test_default_slots_report_null_updated_at_and_author(client):
    listed = next(
        item
        for item in _post(client, "list", {"mem_cube_id": CUBE}).json()["data"]["prompts"]
        if item["key"] == "dream.motive"
    )
    state = listed["state"]["en"]
    assert "updated_at" in state and state["updated_at"] is None
    assert "author" in state and state["author"] is None

    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "dream.motive"}).json()["data"]
    effective = detail["effective"]["en"]
    assert effective["source"] == "default"
    assert "updated_at" in effective and effective["updated_at"] is None
    assert "author" in effective and effective["author"] is None

    # A save that ends up on the built-in body reports the same shape.
    project = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "甲 ${conversation}",
            "author": "alice",
            "base_version_id": 0,
        },
    )
    assert project.status_code == 200, project.text
    reset = _post(
        client, "reset", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "author": "bob"}
    )
    assert reset.status_code == 200, reset.text
    reset_version_id = reset.json()["data"]["reset"][0]["version_id"]

    back_to_default = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "restore_from_version_id": reset_version_id,
            "base_version_id": 0,
        },
    )
    assert back_to_default.status_code == 200, back_to_default.text
    saved = back_to_default.json()["data"]["effective"]
    assert saved["source"] == "default"
    assert "updated_at" in saved and saved["updated_at"] is None
    assert "author" in saved and saved["author"] is None


MALFORMED_BODIES = [
    "没配对的花括号 {memories_block",
    "{",
    "}",
    "{{{",
    "{memories_block.foo}",
    "{memories_block[foo]}",
    "{memories_block[0]}",
    "{memories_block!r}",
    "{memories_block:{memories_block}}",
    "{0}",
    "${",
    "${}",
    "x" * 30000,
    "含 NUL 的正文 \x00 结束",
    "孤立代理项 \ud800 结束",
]


def test_validate_reports_bad_field_names_instead_of_a_500(client):
    cases = [
        ("{memories_block.foo}", "unsupported_field"),
        ("{memories_block[foo]}", "unsupported_field"),
        ("{memories_block[0]}", "unsupported_field"),
        ("{0}", "unsupported_field"),
        ("{memories_block!r}", "unsupported_conversion"),
        ("{memories_block:>10}", "unsupported_format_spec"),
    ]
    for body, code in cases:
        response = _post(client, "validate", {"key": "dream.motive", "lang": "en", "body": body})
        assert response.status_code == 200, (body, response.text)
        data = response.json()["data"]
        assert data["ok"] is False, body
        assert code in [error["code"] for error in data["errors"]], (body, data["errors"])
        assert "object has no attribute" not in response.text
        assert "unsupported operand" not in response.text


def _post_raw(client, name: str, payload: dict):
    """Send the JSON ourselves: a lone surrogate cannot be UTF-8 encoded as-is."""
    return client.post(
        f"/product/prompts/{name}",
        content=json.dumps(payload, ensure_ascii=True).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.parametrize(
    ("key", "lang"),
    [
        ("extract.chat", "zh"),
        ("process.hallucination_filter", "en"),
        ("dream.motive", "en"),
    ],
)
def test_validate_and_save_never_500_for_malformed_bodies(client, mock_init_server, key, lang):
    raw = TestClient(mock_init_server, raise_server_exceptions=False)
    for body in MALFORMED_BODIES:
        validated = _post_raw(raw, "validate", {"key": key, "lang": lang, "body": body})
        assert validated.status_code == 200, (key, repr(body), validated.text)
        assert "code" in validated.json()

        saved = _post_raw(
            raw, "save", {"mem_cube_id": CUBE, "key": key, "lang": lang, "body": body}
        )
        assert saved.status_code in (200, 400), (key, repr(body), saved.status_code, saved.text)
        assert "code" in saved.json()


@pytest.mark.parametrize(
    "route", ["list", "detail", "versions", "validate", "save", "reset", "test", "generate"]
)
def test_missing_required_fields_are_422_not_500(client, mock_init_server, route):
    raw = TestClient(mock_init_server, raise_server_exceptions=False)
    resp = _post_raw(raw, route, {})
    assert resp.status_code == 422, (route, resp.status_code, resp.text)
    assert resp.json()["code"] == 422


def test_a_project_cannot_restore_another_projects_version(client):
    secret = "XA-SECRET-BODY ${conversation}"
    saved = _post(
        client,
        "save",
        {"mem_cube_id": "cube-xa", "key": "extract.chat", "lang": "zh", "body": secret},
    ).json()["data"]

    stolen = _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xb",
            "key": "extract.chat",
            "lang": "zh",
            "restore_from_version_id": saved["version_id"],
        },
    )
    assert stolen.status_code == 404
    assert stolen.json()["code"] == "unknown_version"
    assert "XA-SECRET-BODY" not in stolen.text
    assert "cube-xa" not in stolen.text

    other = _post(client, "detail", {"mem_cube_id": "cube-xb", "key": "extract.chat"}).json()[
        "data"
    ]
    assert other["effective"]["zh"]["source"] == "default"

    own = _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xa",
            "key": "extract.chat",
            "lang": "zh",
            "restore_from_version_id": saved["version_id"],
            "base_version_id": saved["version_id"],
        },
    )
    assert own.status_code == 200, own.text
    assert own.json()["data"]["effective"]["body"] == secret


def test_version_pagination_does_not_accept_another_projects_version_id(client):
    first = _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xa",
            "key": "extract.chat",
            "lang": "zh",
            "body": "甲 ${conversation}",
        },
    ).json()["data"]
    second = _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xa",
            "key": "extract.chat",
            "lang": "zh",
            "body": "乙 ${conversation}",
            "base_version_id": first["version_id"],
        },
    ).json()["data"]
    b_first = _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xb",
            "key": "extract.chat",
            "lang": "zh",
            "body": "丙 ${conversation}",
        },
    ).json()["data"]
    _post(
        client,
        "save",
        {
            "mem_cube_id": "cube-xb",
            "key": "extract.chat",
            "lang": "zh",
            "body": "丁 ${conversation}",
            "base_version_id": b_first["version_id"],
        },
    )

    # Paging with A's newest version id only sees B's own rows below it.
    page = _post(
        client,
        "versions",
        {
            "mem_cube_id": "cube-xb",
            "key": "extract.chat",
            "lang": "zh",
            "before_version_id": second["version_id"],
        },
    )
    assert page.status_code == 200, page.text
    assert page.json()["data"]["versions"] == []
    assert "甲 ${conversation}" not in page.text
    assert "乙 ${conversation}" not in page.text

    own_page = _post(
        client,
        "versions",
        {
            "mem_cube_id": "cube-xb",
            "key": "extract.chat",
            "lang": "zh",
            "before_version_id": b_first["version_id"] + 1,
        },
    ).json()["data"]["versions"]
    assert [version["body"] for version in own_page] == ["丙 ${conversation}"]


def test_reset_items_report_the_version_no_of_the_new_row(client):
    first = _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "甲 ${conversation}"},
    ).json()["data"]
    second = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "乙 ${conversation}",
            "base_version_id": first["version_id"],
        },
    ).json()["data"]
    _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "restore_from_version_id": first["version_id"],
            "base_version_id": second["version_id"],
        },
    )

    reset = _post(client, "reset", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"})
    item = reset.json()["data"]["reset"][0]
    assert item["version_no"] == 4

    versions = _post(
        client, "versions", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    ).json()["data"]["versions"]
    assert versions[0]["version_no"] == item["version_no"]
    assert versions[0]["kind"] == "reset"

    # The slot itself is back on the default, so its state stays at 0.
    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"}).json()["data"]
    assert detail["effective"]["zh"]["version_no"] == 0

    # A whole-project reset reports the same slot-local number for every slot.
    _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "丙 ${conversation}",
            "base_version_id": 0,
        },
    )
    _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "en", "body": "en ${conversation}"},
    )
    everything = _post(client, "reset", {"mem_cube_id": CUBE}).json()["data"]["reset"]
    assert {(item["key"], item["lang"]) for item in everything} == {
        ("extract.chat", "zh"),
        ("extract.chat", "en"),
    }
    # Counting is per slot: zh already has five rows, en only has its first save.
    expected = {"zh": 6, "en": 2}
    for item in everything:
        latest = _post(
            client,
            "versions",
            {"mem_cube_id": CUBE, "key": item["key"], "lang": item["lang"]},
        ).json()["data"]["versions"][0]
        assert item["version_no"] == latest["version_no"] == expected[item["lang"]]


def test_save_rejects_an_invalid_body_with_errors(client):
    response = _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "没有占位符"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_prompt"
    assert body["data"]["errors"][0]["code"] == "missing_placeholder"


def test_save_reports_a_version_conflict_with_the_current_version(client):
    _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "甲 ${conversation}"},
    )
    response = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "乙 ${conversation}",
            "base_version_id": 0,
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "version_conflict"
    assert body["data"]["current"]["version_id"] == 1


def test_save_rejects_an_overlong_note(client):
    response = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "甲 ${conversation}",
            "note": "x" * 201,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"


def test_save_can_roll_back_to_an_old_version(client):
    _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "甲 ${conversation}"},
    )
    _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "乙 ${conversation}",
            "base_version_id": 1,
        },
    )
    rolled = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "restore_from_version_id": 1,
            "base_version_id": 2,
        },
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["data"]["effective"]["body"] == "甲 ${conversation}"

    versions = _post(
        client, "versions", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    ).json()["data"]["versions"]
    assert versions[0]["kind"] == "rollback"


def test_reset_one_slot_and_then_the_whole_project(client):
    for lang in ("zh", "en"):
        _post(
            client,
            "save",
            {
                "mem_cube_id": CUBE,
                "key": "extract.chat",
                "lang": lang,
                "body": f"{lang} ${{conversation}}",
            },
        )
    _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "dream.motive",
            "lang": "en",
            "body": "{memories_block} {max_motives}",
        },
    )

    single = _post(
        client, "reset", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    ).json()["data"]["reset"]
    assert [(item["key"], item["lang"]) for item in single] == [("extract.chat", "zh")]

    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"}).json()["data"]
    assert detail["effective"]["zh"]["source"] == "default"
    assert detail["effective"]["en"]["source"] == "project"

    everything = _post(client, "reset", {"mem_cube_id": CUBE}).json()["data"]["reset"]
    assert {(item["key"], item["lang"]) for item in everything} == {
        ("extract.chat", "en"),
        ("dream.motive", "en"),
    }
    again = _post(client, "reset", {"mem_cube_id": CUBE}).json()["data"]["reset"]
    assert again == []


def test_legacy_endpoints_share_the_same_slot(client):
    saved = client.post(
        "/product/save_extract_prompt",
        json={"mem_cube_id": CUBE, "prompt": "旧接口模板 ${conversation}"},
    )
    assert saved.status_code == 200, saved.text

    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"}).json()["data"]
    assert detail["effective"]["zh"]["body"] == "旧接口模板 ${conversation}"
    assert detail["effective"]["en"]["body"] == "旧接口模板 ${conversation}"
    assert detail["effective"]["zh"]["version_id"] == 1

    versions = _post(
        client, "versions", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "en"}
    ).json()["data"]["versions"]
    assert versions[0]["author"] == "legacy"

    client.post("/product/reset_extract_prompt", json={"mem_cube_id": CUBE})
    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"}).json()["data"]
    assert detail["effective"]["zh"]["source"] == "default"
    assert detail["effective"]["en"]["source"] == "default"


def test_version_no_is_reported_wherever_a_version_is(client):
    first = _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "甲 ${conversation}"},
    ).json()["data"]
    assert first["version_no"] == 1
    assert first["effective"]["version_no"] == 1

    second = _post(
        client,
        "save",
        {
            "mem_cube_id": CUBE,
            "key": "extract.chat",
            "lang": "zh",
            "body": "乙 ${conversation}",
            "base_version_id": first["version_id"],
        },
    ).json()["data"]
    assert second["version_no"] == 2

    conflict = _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "丙 ${conversation}"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["data"]["current"]["version_no"] == 2

    reset = _post(client, "reset", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"})
    reset_item = reset.json()["data"]["reset"][0]
    assert reset_item["version_no"] == 3

    detail = _post(client, "detail", {"mem_cube_id": CUBE, "key": "extract.chat"}).json()["data"]
    assert detail["effective"]["zh"]["version_no"] == 0
    assert detail["effective"]["en"]["version_no"] == 0

    chat = next(
        item
        for item in _post(client, "list", {"mem_cube_id": CUBE}).json()["data"]["prompts"]
        if item["key"] == "extract.chat"
    )
    assert chat["state"]["zh"]["version_no"] == 0

    versions = _post(
        client, "versions", {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh"}
    ).json()["data"]["versions"]
    assert [version["version_no"] for version in versions] == [3, 2, 1]


ROUTE_CASES = [
    ("detail", {"key": "nope"}),
    ("versions", {"key": "nope", "lang": "zh"}),
    ("validate", {"key": "nope", "lang": "zh", "body": "x"}),
    ("save", {"key": "nope", "lang": "zh", "body": "x"}),
    ("reset", {"key": "nope", "lang": "zh"}),
    ("test", {"key": "nope", "lang": "zh", "input": {}}),
    ("generate", {"key": "nope", "lang": "zh", "instruction": "改一下"}),
]


@pytest.mark.parametrize(("route", "payload"), ROUTE_CASES)
def test_an_unknown_key_is_a_404_on_every_route(client, route, payload):
    response = _post(client, route, {"mem_cube_id": CUBE, **payload})
    assert response.status_code == 404, (route, response.text)
    assert response.json()["code"] == "unknown_prompt_key", (route, response.text)


def test_an_unexpected_error_is_a_fixed_internal_error(client, mock_init_server, monkeypatch):
    handler = __import__(
        "memos.api.routers.server_router", fromlist=["prompt_handler"]
    ).prompt_handler

    def boom(_request):
        raise RuntimeError("postgres://user:secret@10.0.0.1 db is gone")

    monkeypatch.setattr(handler, "list_prompts", boom)
    response = TestClient(mock_init_server).post(
        "/product/prompts/list", json={"mem_cube_id": CUBE}
    )
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "secret" not in response.text
    assert "10.0.0.1" not in response.text


def test_a_locked_prompt_database_answers_busy(client, monkeypatch):
    import sqlite3

    from memos.mem_reader import prompt_store

    monkeypatch.setenv("MEMOS_PROMPT_DB_TIMEOUT", "0.2")
    _post(
        client,
        "save",
        {"mem_cube_id": CUBE, "key": "extract.chat", "lang": "zh", "body": "甲 ${conversation}"},
    )

    blocker = sqlite3.connect(prompt_store._db_path(), timeout=5)
    blocker.isolation_level = None
    blocker.execute("BEGIN IMMEDIATE")
    try:
        response = _post(
            client,
            "save",
            {
                "mem_cube_id": CUBE,
                "key": "extract.chat",
                "lang": "zh",
                "body": "乙 ${conversation}",
            },
        )
        assert response.status_code == 429, response.text
        assert response.json()["code"] == "busy"
        assert response.json()["data"]["retry_after_sec"] >= 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
