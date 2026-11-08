"""The product client must preserve scope and never replay or leak credentials."""

import json
import threading

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from memos.skill_experience.client import MemoryClientError, ProductMemoryClient


@contextmanager
def server(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size)
            requests.append((self.command, self.path, json.loads(body) if body else None))
            status, headers, content = responses[min(len(requests) - 1, len(responses) - 1)]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

        do_GET = respond  # noqa: N815 - stdlib HTTP handler contract
        do_POST = respond  # noqa: N815 - stdlib HTTP handler contract

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def response(data):
    return (
        200,
        {"Content-Type": "application/json"},
        json.dumps({"code": 200, "data": data}).encode(),
    )


def test_client_uses_product_paths_scoped_read_and_exact_metadata_filter(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    with server([response([]), response(None), response({"text_mem": []})]) as (url, requests):
        client = ProductMemoryClient(url)
        client.add({"async_mode": "sync"})
        client.get("record/1", "cube-a")
        client.find("cube-a", "owner-a", {"skill_operation_id": "operation", "user_id": "owner-a"})
    assert requests[0] == ("POST", "/product/add", {"async_mode": "sync"})
    assert (
        requests[1][1] == "/product/get_memory/record%2F1?mem_cube_id=cube-a&include_sources=true"
    )
    assert requests[2][2] == {
        "mem_cube_id": "cube-a",
        "user_id": "owner-a",
        "include_sources": True,
        "include_memory_view": ["detail_factual"],
        "page": 1,
        "page_size": None,
        "filter": {"and": [{"skill_operation_id": "operation"}, {"user_id": "owner-a"}]},
    }
    assert url not in client.service_identity


def test_redirect_is_not_followed_or_retried_and_error_does_not_echo_auth():
    secret = "synthetic-private-token"
    with server([(307, {"Location": "http://127.0.0.1:1/leak"}, secret.encode())]) as (
        url,
        requests,
    ):
        client = ProductMemoryClient(url, headers={"Authorization": f"Bearer {secret}"})
        with pytest.raises(MemoryClientError) as error:
            client.add({"safe": True})
        assert len(requests) == 1
        assert secret not in str(error.value)
        assert url not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b'{"code":200,"code":400,"data":null}',
        b"not json",
        b'{"code":400,"message":"secret"}',
        b'{"code":200,"data":NaN}',
    ],
)
def test_invalid_or_rejected_response_fails_closed(content):
    with server([(200, {}, content)]) as (url, _), pytest.raises(MemoryClientError):
        ProductMemoryClient(url).get("id", "cube")


def test_response_limit_is_enforced_before_parsing():
    with (
        server([(200, {}, b"x" * (2 * 1024 * 1024 + 1))]) as (url, _),
        pytest.raises(MemoryClientError, match="response_too_large"),
    ):
        ProductMemoryClient(url).get("id", "cube")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/x",
        "http://user:secret@localhost",
        "https://host/?token=secret",
        "https://host/#fragment",
    ],
)
def test_unsafe_url_config_is_rejected_without_echo(url):
    with pytest.raises(ValueError, match="invalid_service_url"):
        ProductMemoryClient(url)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), 0, -1, True])
def test_invalid_timeout_is_rejected_before_a_delivery_can_be_claimed(timeout):
    with pytest.raises(ValueError, match="invalid_timeout"):
        ProductMemoryClient("http://127.0.0.1:1", timeout=timeout)
