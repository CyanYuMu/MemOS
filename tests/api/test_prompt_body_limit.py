"""G4: the body limit must be enforced before the body is read.

These go through the real dev app (``scripts/dev/prompt_service.py``): its
routes are the same ones the data plane serves, and it is the only prompt app
this machine can build without Qdrant / Neo4j.
"""

import asyncio
import importlib.util
import json

from pathlib import Path

import pytest

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
JSON_HEADERS = {"content-type": "application/json"}


def _dev_app():
    path = ROOT / "scripts" / "dev" / "prompt_service.py"
    spec = importlib.util.spec_from_file_location("prompt_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_app(llm=None)


@pytest.fixture(scope="module")
def dev_app():
    return _dev_app()


@pytest.fixture(autouse=True)
def prompt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOS_PROMPT_CONFIG_DB", str(tmp_path / "body-limit.db"))


def _big_body(chunks: int = 40, size: int = 20 * 1024):
    yield b'{"mem_cube_id": "cube-body", "key": "extract.chat", "lang": "zh", "body": "'
    for _ in range(chunks):
        yield b"x" * size
    yield b' $"\n}'


def test_a_chunked_body_over_the_limit_is_rejected(dev_app):
    client = TestClient(dev_app, raise_server_exceptions=False)
    response = client.post("/product/prompts/save", content=_big_body(), headers=JSON_HEADERS)
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "input_too_large"


def test_the_smaller_limit_still_applies_to_test(dev_app):
    client = TestClient(dev_app, raise_server_exceptions=False)
    # 300 KiB sits between the 256 KiB test limit and the 512 KiB default one.
    payload = json.dumps(
        {
            "mem_cube_id": "cube-body",
            "key": "extract.chat",
            "lang": "zh",
            "input": {"messages": [{"role": "user", "content": "x" * (300 * 1024)}]},
        }
    ).encode()
    response = client.post("/product/prompts/test", content=payload, headers=JSON_HEADERS)
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "input_too_large"


def test_a_small_body_still_reaches_the_handler(dev_app):
    client = TestClient(dev_app, raise_server_exceptions=False)
    response = client.post(
        "/product/prompts/validate",
        json={"key": "extract.chat", "lang": "zh", "body": "只抽部署 ${conversation}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["ok"] is True


def _run_asgi(app, path: str, chunks: list[bytes], headers: dict[str, str]):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
    }
    pending = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    received = {"bytes": 0}
    sent: list[dict] = []

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        message = pending.pop(0)
        received["bytes"] += len(message["body"])
        return message

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent, received["bytes"]


def test_a_content_length_that_lies_does_not_skip_the_check(dev_app):
    # The client claims 10 bytes but streams 1 MiB.
    sent, read_bytes = _run_asgi(
        dev_app,
        "/product/prompts/save",
        [_big_body().__iter__().__next__(), *[b"x" * (64 * 1024)] * 16, b'"}'],
        {**JSON_HEADERS, "content-length": "10"},
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    assert json.loads(body)["code"] == "input_too_large"
    # It answered before reading the whole body.
    assert read_bytes < 1024 * 1024
