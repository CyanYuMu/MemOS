"""Host probes distinguish drift, unavailable data and broken identity."""

import copy

from pathlib import Path

from memos.skill_lifecycle.observers import (
    memory_subject,
    observe_memory,
    observe_tool,
    tool_subject,
)


class Memory:
    service_identity = "a" * 64

    def __init__(self):
        self.record = {
            "id": "memory-one",
            "memory": "original",
            "metadata": {"user_id": "owner", "skill_namespace": "ns"},
        }
        self.error = None

    def get(self, memory_id, cube_id):
        assert (memory_id, cube_id) == ("memory-one", "cube")
        if self.error:
            raise self.error
        return {"data": copy.deepcopy(self.record)}


def source(client):
    return memory_subject(
        {
            "service_identity": client.service_identity,
            "cube_id": "cube",
            "user_id": "owner",
            "namespace": "ns",
            "memory_id": "memory-one",
            "memory": "original",
            "metadata": copy.deepcopy(client.record["metadata"]),
            "snapshot_id": "b" * 64,
        }
    )


def test_memory_real_read_distinguishes_change_failure_and_invalid_owner():
    client = Memory()
    subject = source(client)
    assert observe_memory(client, subject)["status"] == "healthy"
    client.record["memory"] = "changed"
    assert observe_memory(client, subject)["status"] == "changed"
    client.record["metadata"]["user_id"] = "intruder"
    assert observe_memory(client, subject)["status"] == "invalid"
    client.error = OSError("private-host-secret")
    assert observe_memory(client, subject) == {
        "subjectId": subject["subjectId"],
        "status": "unavailable",
        "observedDigest": None,
        "reason": "memory_unavailable",
    }


def test_tool_detects_same_version_different_bytes(tmp_path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho 'Claude Code 1.0'\n")
    cli.chmod(0o700)
    subject = tool_subject(cli, implementation_paths=[Path(__file__)])
    assert observe_tool(subject)["status"] == "healthy"
    cli.write_text("#!/bin/sh\n# changed implementation\necho 'Claude Code 1.0'\n")
    assert observe_tool(subject)["status"] == "changed"
    cli.unlink()
    assert observe_tool(subject)["status"] == "unavailable"


def test_memory_probe_uses_real_scoped_http_and_handles_invalid_json():
    import json
    import threading

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from memos.skill_experience.client import ProductMemoryClient

    state = {
        "record": {
            "id": "memory-one",
            "memory": "original",
            "metadata": {"user_id": "owner", "skill_namespace": "ns"},
        },
        "invalid": False,
        "paths": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - standard HTTP callback
            state["paths"].append(self.path)
            data = (
                b"not-json"
                if state["invalid"]
                else json.dumps({"code": 200, "data": state["record"]}).encode()
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ProductMemoryClient("http://127.0.0.1:" + str(server.server_port), timeout=0.2)
    subject = memory_subject(
        {
            "service_identity": client.service_identity,
            "cube_id": "cube",
            "user_id": "owner",
            "namespace": "ns",
            "memory_id": "memory-one",
            "memory": "original",
            "metadata": copy.deepcopy(state["record"]["metadata"]),
            "snapshot_id": "b" * 64,
        }
    )
    try:
        assert observe_memory(client, subject)["status"] == "healthy"
        state["record"]["memory"] = "changed over actual HTTP"
        assert observe_memory(client, subject)["status"] == "changed"
        state["invalid"] = True
        assert observe_memory(client, subject)["status"] == "invalid"
        assert all(
            path == "/product/get_memory/memory-one?mem_cube_id=cube&include_sources=true"
            for path in state["paths"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert observe_memory(client, subject)["status"] == "unavailable"


def test_observer_deduplicates_and_budget_expiry_is_unavailable(monkeypatch):
    import threading
    import time

    import memos.skill_lifecycle.observers as module

    calls = []
    release = threading.Event()

    def slow(client, subject):
        calls.append(subject["subjectId"])
        release.wait(timeout=1)
        return {
            "subjectId": subject["subjectId"],
            "status": "healthy",
            "observedDigest": subject["expectedDigest"],
            "reason": "memory_matches",
        }

    monkeypatch.setattr(module, "observe_memory", slow)
    subject = {"subjectId": "a" * 64, "kind": "memory", "expectedDigest": "b" * 64}
    observer = module.HostObserver(object(), budget_seconds=0.03, max_workers=2)
    started = time.monotonic()
    try:
        results = observer([subject, subject])
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
    assert results[0] == results[1]
    assert (
        results[0]["status"] == "unavailable" and results[0]["reason"] == "probe_budget_exhausted"
    )
    assert calls == ["a" * 64]
